from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class Strategy(Protocol):
    name: str

    async def on_h4_close(self, candle: dict) -> None:
        ...

    async def on_m15_close(self, candle: dict) -> None:
        ...


@dataclass
class StrategyContext:
    symbol: str
    client: any
