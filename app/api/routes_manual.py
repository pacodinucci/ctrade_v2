from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.broker import get_broker
from app.broker.base import Side
from app.db.repository import TradeCreateInput
from app.broker.ctrader_market_data import (
    get_open_positions as md_get_open_positions,
    get_symbol_point_size as md_get_symbol_point_size,
    has_open_position as md_has_open_position,
    open_market_order as md_open_market_order,
    set_position_sl_tp as md_set_position_sl_tp,
)
from app.services.sltp import CTraderOrderError, validate_sl_tp_for_side

router = APIRouter(prefix="/manual", tags=["manual"])


def _get_trade_repository():
    from app.main import bot_repository  # circular safe

    return bot_repository


def _http_error(status: int, error: str, detail: str, *, position_id: int | None = None) -> HTTPException:
    payload: dict[str, object] = {"error": error, "detail": detail}
    if position_id is not None:
        payload["position_id"] = int(position_id)
    return HTTPException(status_code=status, detail=payload)


def _to_float_or_none(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _to_iso_utc(timestamp_value: object) -> str | None:
    try:
        if timestamp_value is None:
            return None
        ts = int(timestamp_value)
    except Exception:
        return None

    # Heuristica: OpenAPI puede venir en segundos, milisegundos o microsegundos.
    if ts > 10**14:
        seconds = ts / 1_000_000.0
    elif ts > 10**11:
        seconds = ts / 1_000.0
    else:
        seconds = float(ts)

    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _side_from_trade_side(trade_side: object) -> str | None:
    try:
        side_int = int(trade_side)
    except Exception:
        return None
    if side_int == 1:
        return "buy"
    if side_int == 2:
        return "sell"
    return None


class ManualOpenOrderRequest(BaseModel):
    symbol: str = Field(..., description="Simbolo, ej: EURUSD")
    side: Side = Field(..., description="buy | sell")
    volume: float = Field(..., gt=0, description="Volumen en unidades Open API (ej: 100, 1000, 100000)")
    stop_loss: Optional[float] = Field(None, description="Precio absoluto de Stop Loss (opcional).")
    take_profit: Optional[float] = Field(None, description="Precio absoluto de Take Profit (opcional).")


class ManualOpenWithPointsRequest(BaseModel):
    symbol: str = Field(..., description="Simbolo, ej: EURUSD")
    side: Side = Field(..., description="buy | sell")
    volume: float = Field(..., gt=0, description="Volumen en unidades Open API (ej: 100, 1000, 100000)")
    sl_points: Optional[float] = Field(None, description="Distancia de SL en puntos desde la entrada real.")
    tp_points: Optional[float] = Field(None, description="Distancia de TP en puntos desde la entrada real.")


class ManualUpdateStopsRequest(BaseModel):
    position_id: int = Field(..., description="PositionId de cTrader")
    stop_loss: Optional[float] = Field(None, description="Precio absoluto de SL. null para quitar.")
    take_profit: Optional[float] = Field(None, description="Precio absoluto de TP. null para quitar.")


class ManualCloseOrderRequest(BaseModel):
    position_id: int
    volume: Optional[float] = Field(None, description="Volumen a cerrar. Si es null, cierra el 100%.")


async def _find_open_position(position_id: int) -> dict | None:
    positions = await md_get_open_positions()
    for item in positions:
        if int(item.get("position_id") or 0) == int(position_id):
            return item
    return None


async def _resolve_position_from_reconcile(
    *,
    symbol: str,
    side: Side,
    max_attempts: int = 10,
    sleep_sec: float = 0.35,
) -> dict | None:
    desired_trade_side = 1 if str(side).lower() == "buy" else 2

    for _ in range(max_attempts):
        positions = await md_get_open_positions()
        candidates = [
            p
            for p in positions
            if str(p.get("symbol") or "").upper() == symbol
            and int(p.get("trade_side") or 0) == desired_trade_side
            and p.get("position_id") is not None
        ]
        if candidates:
            candidates.sort(key=lambda p: int(p.get("open_timestamp") or 0), reverse=True)
            return candidates[0]
        await asyncio.sleep(sleep_sec)

    return None


async def _open_manual_position(*, symbol: str, side: Side, volume: float) -> dict[str, object]:
    print(
        "[manual] manual_open_bridge_payload "
        f"symbol={symbol} side={str(side).lower().strip()} volume={volume}"
    )
    opened = await md_open_market_order(symbol=symbol, side=side, volume=volume)

    position_id = opened.get("position_id")
    entry_price = _to_float_or_none(opened.get("entry_price"))
    fill_ts: object = None
    raw = opened.get("raw")
    if isinstance(raw, dict):
        fill_ts = raw.get("timestamp")

    if position_id is None or entry_price is None or entry_price <= 0:
        resolved = await _resolve_position_from_reconcile(symbol=symbol, side=side)
        if resolved is None:
            raise _http_error(
                500,
                "ENTRY_NOT_RESOLVED",
                "Orden enviada, pero no se pudo resolver position_id/entry_price via reconcile.",
            )

        position_id = int(resolved.get("position_id"))
        entry_price = _to_float_or_none(resolved.get("open_price"))
        fill_ts = resolved.get("open_timestamp")
        opened["position_id"] = position_id
        opened["entry_price"] = entry_price
        if resolved.get("volume") is not None:
            opened["volume"] = float(resolved.get("volume"))

    if position_id is None or entry_price is None or entry_price <= 0:
        raise _http_error(500, "ENTRY_INVALID", "No se pudo confirmar precio de entrada real.")

    return {
        "position_id": int(position_id),
        "entry_price": float(entry_price),
        "filled_at": _to_iso_utc(fill_ts),
        "opened": opened,
    }


async def _apply_manual_sl_tp_with_verification(
    *,
    position_id: int,
    symbol: str,
    expected_sl: float | None,
    expected_tp: float | None,
    verify_sl: bool,
    verify_tp: bool,
    sleep_sec: float = 0.35,
    max_retries: int = 2,
) -> tuple[dict | None, str | None, dict | None]:
    point = await md_get_symbol_point_size(symbol)
    tolerance = max(point * 1.2, 1e-6)
    last_result: dict | None = None
    last_error: str | None = None

    attempts = max_retries + 1
    for attempt in range(attempts):
        try:
            print(
                "[manual] manual_open_bridge_amend_payload "
                f"position_id={position_id} symbol={symbol} attempt={attempt + 1}/{attempts} "
                f"verify_sl={verify_sl} verify_tp={verify_tp} "
                f"stop_loss={expected_sl if verify_sl else '<unchanged>'} "
                f"take_profit={expected_tp if verify_tp else '<unchanged>'}"
            )
            last_result = await md_set_position_sl_tp(
                symbol=symbol,
                position_id=int(position_id),
                stop_loss=expected_sl if (verify_sl and expected_sl is not None) else None,
                take_profit=expected_tp if (verify_tp and expected_tp is not None) else None,
                clear_stop_loss=bool(verify_sl and expected_sl is None),
                clear_take_profit=bool(verify_tp and expected_tp is None),
            )
        except CTraderOrderError as exc:
            last_error = repr(exc)
            if not exc.retryable:
                raise
        except Exception as exc:
            last_error = repr(exc)

        target = await _find_open_position(int(position_id))
        if target is not None:
            actual_sl = _to_float_or_none(target.get("stop_loss"))
            actual_tp = _to_float_or_none(target.get("take_profit"))

            sl_ok = True
            tp_ok = True
            if verify_sl:
                if expected_sl is None:
                    sl_ok = actual_sl is None
                else:
                    sl_ok = actual_sl is not None and abs(actual_sl - float(expected_sl)) <= tolerance
            if verify_tp:
                if expected_tp is None:
                    tp_ok = actual_tp is None
                else:
                    tp_ok = actual_tp is not None and abs(actual_tp - float(expected_tp)) <= tolerance

            if sl_ok and tp_ok:
                return last_result, last_error, target

        if attempt < (attempts - 1):
            await asyncio.sleep(sleep_sec * (2**attempt))

    raise RuntimeError(
        f"No se pudo confirmar SL/TP tras {attempts} intentos "
        f"(position_id={position_id}, symbol={symbol}, sl={expected_sl}, tp={expected_tp}, "
        f"last_error={last_error})"
    )


@router.post("/open")
async def manual_open_order(body: ManualOpenOrderRequest):
    symbol_u = body.symbol.strip().upper()
    side_u = str(body.side).lower().strip()
    print(
        "[manual] manual_open_request "
        f"symbol={symbol_u} side={side_u} volume={body.volume} "
        f"stop_loss={body.stop_loss} take_profit={body.take_profit}"
    )

    if await md_has_open_position(symbol_u, None):
        raise _http_error(
            409,
            "POSITION_EXISTS",
            f"Ya existe una posicion abierta para {symbol_u}. No se permite mas de una por instrumento.",
        )

    try:
        opened_info = await _open_manual_position(symbol=symbol_u, side=body.side, volume=body.volume)
    except HTTPException:
        raise
    except Exception as exc:
        raise _http_error(500, "OPEN_FAILED", f"Error al abrir operacion manual: {exc!r}") from exc

    position_id = int(opened_info["position_id"])
    entry_price = float(opened_info["entry_price"])

    verify_sl = body.stop_loss is not None
    verify_tp = body.take_profit is not None
    final_position = await _find_open_position(position_id)

    if verify_sl or verify_tp:
        point = await md_get_symbol_point_size(symbol_u)
        try:
            validate_sl_tp_for_side(
                side=side_u,
                entry_price=entry_price,
                stop_loss=body.stop_loss,
                take_profit=body.take_profit,
                min_distance=max(point, 1e-6),
            )
        except ValueError as exc:
            raise _http_error(422, "SL_TP_INVALID", str(exc), position_id=position_id) from exc

        try:
            _, _, final_position = await _apply_manual_sl_tp_with_verification(
                position_id=position_id,
                symbol=symbol_u,
                expected_sl=body.stop_loss,
                expected_tp=body.take_profit,
                verify_sl=verify_sl,
                verify_tp=verify_tp,
            )
        except CTraderOrderError as exc:
            raise _http_error(
                422,
                "SL_TP_INVALID",
                f"cTrader rechazo el AMEND ({exc.error_code}): {exc.description}",
                position_id=position_id,
            ) from exc
        except RuntimeError as exc:
            raise _http_error(500, "SL_TP_APPLY_FAILED", str(exc), position_id=position_id) from exc

    applied_sl = _to_float_or_none((final_position or {}).get("stop_loss"))
    applied_tp = _to_float_or_none((final_position or {}).get("take_profit"))
    print(
        "[manual] manual_open_result "
        f"position_id={position_id} symbol={symbol_u} side={side_u} "
        f"entry={entry_price} sl_applied={applied_sl} tp_applied={applied_tp}"
    )

    repository = _get_trade_repository()
    if repository is not None:
        try:
            logged_volume = opened_info.get("opened", {}).get("volume") if isinstance(opened_info.get("opened"), dict) else None
            await repository.create_trade(
                TradeCreateInput(
                    position_id=str(position_id),
                    bot_id=None,
                    source="manual",
                    strategy=None,
                    symbol=symbol_u,
                    side=side_u,
                    volume=float(logged_volume if logged_volume is not None else body.volume),
                    open_price=entry_price,
                    stop_loss=applied_sl,
                    take_profit=applied_tp,
                    metadata={"endpoint": "/manual/open"},
                )
            )
        except Exception as exc:
            print(f"[manual] trade log create failed for {position_id}: {exc!r}")

    return {
        "ok": True,
        "position_id": position_id,
        "symbol": symbol_u,
        "side": side_u,
        "entry_price": entry_price,
        "filled_at": opened_info.get("filled_at"),
        "stop_loss_applied": applied_sl,
        "take_profit_applied": applied_tp,
        # Backward-compatible fields:
        "stop_loss": applied_sl,
        "take_profit": applied_tp,
    }


@router.post("/update-stops")
async def manual_update_stops(body: ManualUpdateStopsRequest):
    provided_sl = "stop_loss" in body.model_fields_set
    provided_tp = "take_profit" in body.model_fields_set
    if not provided_sl and not provided_tp:
        raise _http_error(400, "INVALID_PAYLOAD", "Debes enviar stop_loss y/o take_profit.", position_id=body.position_id)

    target = await _find_open_position(body.position_id)
    if target is None:
        raise _http_error(404, "POSITION_NOT_FOUND", "Posicion no encontrada o ya cerrada.", position_id=body.position_id)

    symbol_u = str(target.get("symbol") or "").upper()
    side = _side_from_trade_side(target.get("trade_side"))
    if not symbol_u or side is None:
        raise _http_error(500, "POSITION_INVALID", "No se pudo resolver symbol/side de la posicion.", position_id=body.position_id)

    entry_price = _to_float_or_none(target.get("open_price"))
    current_sl = _to_float_or_none(target.get("stop_loss"))
    current_tp = _to_float_or_none(target.get("take_profit"))

    candidate_sl = body.stop_loss if provided_sl else current_sl
    candidate_tp = body.take_profit if provided_tp else current_tp

    point = await md_get_symbol_point_size(symbol_u)
    if entry_price is not None and entry_price > 0:
        try:
            validate_sl_tp_for_side(
                side=side,
                entry_price=entry_price,
                stop_loss=candidate_sl,
                take_profit=candidate_tp,
                min_distance=max(point, 1e-6),
            )
        except ValueError as exc:
            raise _http_error(422, "SL_TP_INVALID", str(exc), position_id=body.position_id) from exc

    try:
        _, _, final_position = await _apply_manual_sl_tp_with_verification(
            position_id=body.position_id,
            symbol=symbol_u,
            expected_sl=body.stop_loss,
            expected_tp=body.take_profit,
            verify_sl=provided_sl,
            verify_tp=provided_tp,
        )
    except CTraderOrderError as exc:
        raise _http_error(
            422,
            "SL_TP_INVALID",
            f"cTrader rechazo el AMEND ({exc.error_code}): {exc.description}",
            position_id=body.position_id,
        ) from exc
    except RuntimeError as exc:
        raise _http_error(500, "SL_TP_APPLY_FAILED", str(exc), position_id=body.position_id) from exc

    if final_position is None:
        raise _http_error(409, "POSITION_NOT_MODIFIABLE", "No se pudo confirmar estado final de la posicion.", position_id=body.position_id)

    return {
        "ok": True,
        "position_id": int(body.position_id),
        "symbol": symbol_u,
        "side": side,
        "entry_price": entry_price,
        "stop_loss": _to_float_or_none(final_position.get("stop_loss")),
        "take_profit": _to_float_or_none(final_position.get("take_profit")),
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


@router.post("/open-with-points")
async def manual_open_with_points(body: ManualOpenWithPointsRequest):
    symbol_u = body.symbol.strip().upper()
    side_u = str(body.side).lower().strip()

    sl_points = abs(float(body.sl_points)) if body.sl_points is not None else None
    tp_points = abs(float(body.tp_points)) if body.tp_points is not None else None
    if sl_points is None and tp_points is None:
        raise _http_error(400, "INVALID_PAYLOAD", "Debes enviar sl_points y/o tp_points.")
    if sl_points is not None and sl_points <= 0:
        raise _http_error(400, "INVALID_PAYLOAD", "sl_points debe ser mayor a 0.")
    if tp_points is not None and tp_points <= 0:
        raise _http_error(400, "INVALID_PAYLOAD", "tp_points debe ser mayor a 0.")

    if await md_has_open_position(symbol_u, None):
        raise _http_error(
            409,
            "POSITION_EXISTS",
            f"Ya existe una posicion abierta para {symbol_u}. No se permite mas de una por instrumento.",
        )

    try:
        opened_info = await _open_manual_position(symbol=symbol_u, side=body.side, volume=body.volume)
    except HTTPException:
        raise
    except Exception as exc:
        raise _http_error(500, "OPEN_FAILED", f"Error al abrir operacion manual: {exc!r}") from exc

    position_id = int(opened_info["position_id"])
    entry_price = float(opened_info["entry_price"])

    point = await md_get_symbol_point_size(symbol_u)
    expected_sl: float | None = None
    expected_tp: float | None = None

    if side_u == "buy":
        if sl_points is not None:
            expected_sl = entry_price - (sl_points * point)
        if tp_points is not None:
            expected_tp = entry_price + (tp_points * point)
    else:
        if sl_points is not None:
            expected_sl = entry_price + (sl_points * point)
        if tp_points is not None:
            expected_tp = entry_price - (tp_points * point)

    try:
        validate_sl_tp_for_side(
            side=side_u,
            entry_price=entry_price,
            stop_loss=expected_sl,
            take_profit=expected_tp,
            min_distance=max(point, 1e-6),
        )
    except ValueError as exc:
        raise _http_error(422, "SL_TP_INVALID", str(exc), position_id=position_id) from exc

    try:
        _, _, final_position = await _apply_manual_sl_tp_with_verification(
            position_id=position_id,
            symbol=symbol_u,
            expected_sl=expected_sl,
            expected_tp=expected_tp,
            verify_sl=sl_points is not None,
            verify_tp=tp_points is not None,
        )
    except CTraderOrderError as exc:
        raise _http_error(
            422,
            "SL_TP_INVALID",
            f"cTrader rechazo el AMEND ({exc.error_code}): {exc.description}",
            position_id=position_id,
        ) from exc
    except RuntimeError as exc:
        raise _http_error(500, "SL_TP_APPLY_FAILED", str(exc), position_id=position_id) from exc

    repository = _get_trade_repository()
    if repository is not None:
        try:
            logged_volume = opened_info.get("opened", {}).get("volume") if isinstance(opened_info.get("opened"), dict) else None
            await repository.create_trade(
                TradeCreateInput(
                    position_id=str(position_id),
                    bot_id=None,
                    source="manual",
                    strategy="manual_open_with_points",
                    symbol=symbol_u,
                    side=side_u,
                    volume=float(logged_volume if logged_volume is not None else body.volume),
                    open_price=entry_price,
                    stop_loss=_to_float_or_none((final_position or {}).get("stop_loss")),
                    take_profit=_to_float_or_none((final_position or {}).get("take_profit")),
                    metadata={
                        "endpoint": "/manual/open-with-points",
                        "sl_points": sl_points,
                        "tp_points": tp_points,
                        "point_size": point,
                    },
                )
            )
        except Exception as exc:
            print(f"[manual] trade log create failed for {position_id}: {exc!r}")

    return {
        "ok": True,
        "position_id": position_id,
        "symbol": symbol_u,
        "side": side_u,
        "entry_price": entry_price,
        "filled_at": opened_info.get("filled_at"),
        "point_size": point,
        "sl_points": sl_points,
        "tp_points": tp_points,
        "stop_loss": _to_float_or_none((final_position or {}).get("stop_loss")),
        "take_profit": _to_float_or_none((final_position or {}).get("take_profit")),
    }


@router.post("/close")
async def manual_close_order(body: ManualCloseOrderRequest):
    broker = get_broker()
    close_price: float | None = None

    try:
        positions = await md_get_open_positions()
        for item in positions:
            if int(item.get("position_id") or 0) == int(body.position_id):
                symbol_u = str(item.get("symbol") or "").upper()
                if symbol_u:
                    from app.main import client  # circular safe

                    close_price = float(await client.price(symbol_u))
                break
    except Exception:
        close_price = None

    try:
        result = await broker.close_position(
            position_id=body.position_id,
            volume=body.volume,
        )
    except Exception as exc:
        raise _http_error(500, "CLOSE_FAILED", f"Error al cerrar posicion: {exc!r}", position_id=body.position_id) from exc

    repository = _get_trade_repository()
    if repository is not None:
        try:
            await repository.close_trade(
                position_id=str(body.position_id),
                close_reason="MANUAL_CLOSE",
                close_price=close_price,
                metadata={"endpoint": "/manual/close"},
            )
        except Exception as exc:
            print(f"[manual] trade log close failed for {body.position_id}: {exc!r}")

    return {
        "status": "ok",
        "position_id": body.position_id,
        "requested_volume": body.volume,
        "broker_result": result,
    }
