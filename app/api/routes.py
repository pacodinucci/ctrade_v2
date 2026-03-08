from __future__ import annotations

from fastapi import APIRouter, Depends

from app.bots.manager import BotManager


def get_bot_manager() -> BotManager:
    from app.main import bot_manager  # circular safe
    return bot_manager


router = APIRouter()


@router.get("/bots")
async def list_bots(manager: BotManager = Depends(get_bot_manager)):
    return await manager.list_bots()


@router.post("/bots")
async def create_bot(payload: dict, manager: BotManager = Depends(get_bot_manager)):
    symbol = payload.get("symbol", "EURUSD")
    strategy = payload.get("strategy", "peak_dip")
    bot_id = await manager.create_bot(symbol=symbol, strategy=strategy)
    return {"bot_id": bot_id}
