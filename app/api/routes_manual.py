from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.broker import get_broker
from app.broker.base import Side
from app.broker.ctrader_market_data import (
    close_position as md_close_position,
    get_open_positions as md_get_open_positions,
    has_open_position as md_has_open_position,
    open_market_order as md_open_market_order,
    set_position_sl_tp as md_set_position_sl_tp,
)
from app.strategies.peak_dip.utils import point_size

router = APIRouter(prefix="/manual", tags=["manual"])


class ManualOpenOrderRequest(BaseModel):
    symbol: str = Field(..., description="Simbolo, ej: EURUSD")
    side: Side = Field(..., description="buy | sell")
    volume: float = Field(..., gt=0, description="Volumen en unidades Open API (ej: 100, 1000, 100000)")

    stop_loss: Optional[float] = Field(
        None,
        description="Precio absoluto de Stop Loss. Si es null, se calcula por defecto.",
    )
    take_profit: Optional[float] = Field(
        None,
        description="Precio absoluto de Take Profit. Si es null, se calcula por defecto.",
    )


class ManualCloseOrderRequest(BaseModel):
    position_id: int
    volume: Optional[float] = Field(
        None,
        description="Volumen a cerrar. Si es null, cierra el 100%.",
    )


async def _apply_manual_sl_tp_with_verification(
    *,
    position_id: int,
    expected_sl: float | None,
    expected_tp: float | None,
    symbol: str,
    max_attempts: int = 10,
    sleep_sec: float = 0.35,
) -> tuple[bool, dict | None, str | None]:
    tolerance = max(point_size(symbol.upper()) * 1.2, 1e-6)
    last_result: dict | None = None
    last_error: str | None = None

    for _ in range(max_attempts):
        try:
            last_result = await md_set_position_sl_tp(
                symbol=symbol,
                position_id=int(position_id),
                stop_loss=expected_sl,
                take_profit=expected_tp,
            )
        except Exception as exc:
            last_error = repr(exc)

        positions = await md_get_open_positions()
        target = None
        for p in positions:
            if int(p.get("position_id") or 0) == int(position_id):
                target = p
                break

        if target is not None:
            actual_sl = target.get("stop_loss")
            actual_tp = target.get("take_profit")
            sl_ok = (
                expected_sl is None
                or (actual_sl is not None and abs(float(actual_sl) - float(expected_sl)) <= tolerance)
            )
            tp_ok = (
                expected_tp is None
                or (actual_tp is not None and abs(float(actual_tp) - float(expected_tp)) <= tolerance)
            )
            if bool(sl_ok and tp_ok):
                return True, last_result, last_error

        await asyncio.sleep(sleep_sec)

    return False, last_result, last_error


async def _is_position_closed(position_id: int) -> bool:
    positions = await md_get_open_positions()
    for p in positions:
        if int(p.get("position_id") or 0) == int(position_id):
            return False
    return True


@router.post("/open")
async def manual_open_order(body: ManualOpenOrderRequest):
    """
    Abre una orden de mercado manual y, si es posible, setea SL/TP inmediatamente.
    """
    symbol_u = body.symbol.strip().upper()
    if await md_has_open_position(symbol_u, None):
        raise HTTPException(
            status_code=409,
            detail=f"Ya existe una posicion abierta para {symbol_u}. No se permite mas de una por instrumento.",
        )

    try:
        opened = await md_open_market_order(
            symbol=symbol_u,
            side=body.side,
            volume=body.volume,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Error al abrir operacion manual: {exc!r}",
        ) from exc

    position_id = opened.get("position_id")
    entry_price = opened.get("entry_price")

    # Si no se pudo confirmar entrada, devolvemos el estado parcial.
    if position_id is None or entry_price is None:
        return {
            "status": "partial",
            "detail": "Orden enviada, pero no se pudo confirmar position_id/entry_price. No se aplico SL/TP.",
            "symbol": symbol_u,
            "side": body.side,
            "volume": body.volume,
            "opened": opened,
        }

    sl = body.stop_loss
    tp = body.take_profit

    # Defaults de prueba si no vienen en el payload.
    if sl is None and tp is None:
        if body.side == "buy":
            sl = entry_price - 2.0
            tp = entry_price + 1.0
        else:
            sl = entry_price + 2.0
            tp = entry_price - 1.0

    sl_tp_ok, sl_tp_result, sl_tp_last_error = await _apply_manual_sl_tp_with_verification(
        position_id=int(position_id),
        expected_sl=sl,
        expected_tp=tp,
        symbol=symbol_u,
    )
    if not sl_tp_ok:
        close_volume_raw = opened.get("volume")
        close_volume = float(close_volume_raw) if close_volume_raw is not None else float(body.volume)
        try:
            await md_close_position(position_id=int(position_id), volume=close_volume)
        except Exception:
            pass

        for _ in range(8):
            await asyncio.sleep(0.35)
            if await _is_position_closed(int(position_id)):
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"Operacion abierta (position_id={position_id}) sin SL/TP confirmado; "
                        "se cerro automaticamente para evitar riesgo."
                    ),
                )

        raise HTTPException(
            status_code=500,
            detail=(
                f"Operacion abierta (position_id={position_id}) sin SL/TP confirmado; "
                "no se pudo confirmar cierre automatico. "
                f"Ultimo error de seteo: {sl_tp_last_error or 'N/A'}"
            ),
        )

    return {
        "status": "ok",
        "symbol": symbol_u,
        "side": body.side,
        "volume": body.volume,
        "opened": opened,
        "sl_tp": sl_tp_result,
    }


@router.post("/close")
async def manual_close_order(body: ManualCloseOrderRequest):
    broker = get_broker()

    try:
        result = await broker.close_position(
            position_id=body.position_id,
            volume=body.volume,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Error al cerrar posicion: {exc!r}",
        ) from exc

    return {
        "status": "ok",
        "position_id": body.position_id,
        "requested_volume": body.volume,
        "broker_result": result,
    }
