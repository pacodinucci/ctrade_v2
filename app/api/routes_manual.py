from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.broker import get_broker
from app.broker.base import Side
from app.broker.ctrader_market_data import (
    open_market_order as md_open_market_order,
    set_position_sl_tp as md_set_position_sl_tp,
)

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


@router.post("/open")
async def manual_open_order(body: ManualOpenOrderRequest):
    """
    Abre una orden de mercado manual y, si es posible, setea SL/TP inmediatamente.
    """
    try:
        opened = await md_open_market_order(
            symbol=body.symbol,
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
            "symbol": body.symbol,
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

    try:
        sl_tp_result = await md_set_position_sl_tp(
            symbol=body.symbol,
            position_id=int(position_id),
            stop_loss=sl,
            take_profit=tp,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Operacion abierta (position_id={position_id}), "
                f"pero error al setear SL/TP: {exc!r}"
            ),
        ) from exc

    return {
        "status": "ok",
        "symbol": body.symbol,
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
