from __future__ import annotations

import asyncio
import uuid
from typing import Dict

from app.services.ctrader_client import cTraderClient
from app.strategies.peak_dip.strategy import PeakDipStrategy


class BotManager:
    def __init__(self, client: cTraderClient) -> None:
        self.client = client
        self.bots: Dict[str, PeakDipStrategy] = {}

    async def create_bot(self, *, symbol: str, strategy: str) -> str:
        bot_id = str(uuid.uuid4())
        if strategy != "peak_dip":
            raise ValueError("estrategia no soportada")
        strat = PeakDipStrategy(symbol=symbol, client=self.client)
        self.bots[bot_id] = strat
        return bot_id

    async def list_bots(self) -> dict:
        return {bot_id: strat.name for bot_id, strat in self.bots.items()}

    async def start(self) -> None:
        await self.client.connect()

    async def dispatch_h4(self, candle: dict) -> None:
        await asyncio.gather(*(bot.on_h4_close(candle) for bot in self.bots.values()))

    async def dispatch_m15(self, candle: dict) -> None:
        await asyncio.gather(*(bot.on_m15_close(candle) for bot in self.bots.values()))
