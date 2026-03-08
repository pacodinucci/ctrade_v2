from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List

import pandas as pd

from app.broker.ctrader import CTraderBroker
from app.broker.ctrader_market_data import (
    get_current_price,
    get_trendbars,
)

CandleHandler = Callable[[dict[str, Any]], Awaitable[None]]


class cTraderClient:
    """Facade sobre el broker + market data real de cTrader."""

    def __init__(self) -> None:
        self.broker = CTraderBroker()
        self._h4_handlers: Dict[str, List[CandleHandler]] = {}
        self._m15_handlers: Dict[str, List[CandleHandler]] = {}
        self._poll_tasks: List[asyncio.Task] = []
        self._last_close: Dict[tuple[str, str], pd.Timestamp] = {}

    async def connect(self) -> None:
        # El broker real autentica al instanciarse; acá solo iniciamos polling.
        await self._ensure_pollers()

    async def _ensure_pollers(self) -> None:
        if self._poll_tasks:
            return
        # pollers se activan cuando haya suscripciones
        return

    async def subscribe_h4(self, symbol: str, handler: CandleHandler) -> None:
        await self._register_handler(symbol.upper(), "H4", handler, self._h4_handlers)

    async def subscribe_m15(self, symbol: str, handler: CandleHandler) -> None:
        await self._register_handler(symbol.upper(), "M15", handler, self._m15_handlers)

    async def _register_handler(
        self,
        symbol: str,
        timeframe: str,
        handler: CandleHandler,
        store: Dict[str, List[CandleHandler]],
    ) -> None:
        handlers = store.setdefault(symbol, [])
        handlers.append(handler)
        key = (symbol, timeframe)
        if not any(task.get_name() == f"poll-{symbol}-{timeframe}" for task in self._poll_tasks):
            task = asyncio.create_task(self._poll_candles(symbol, timeframe), name=f"poll-{symbol}-{timeframe}")
            self._poll_tasks.append(task)

    async def _poll_candles(self, symbol: str, timeframe: str) -> None:
        interval = 60 if timeframe == "M15" else 300
        store = self._h4_handlers if timeframe == "H4" else self._m15_handlers
        while True:
            try:
                df = await get_trendbars(symbol, timeframe, count=3)
                if df.empty:
                    await asyncio.sleep(interval)
                    continue
                last_row = df.iloc[-1]
                last_time = pd.Timestamp(last_row["time"], tz="UTC")
                key = (symbol, timeframe)
                prev = self._last_close.get(key)
                if prev is None or last_time > prev:
                    candle = {
                        "symbol": symbol,
                        "time_utc": last_time,
                        "open": float(last_row["open"]),
                        "high": float(last_row["high"]),
                        "low": float(last_row["low"]),
                        "close": float(last_row["close"]),
                    }
                    handlers = store.get(symbol, [])
                    for handler in handlers:
                        await handler(candle)
                    self._last_close[key] = last_time
            except Exception as exc:
                print(f"[cTraderClient] Error polling {symbol} {timeframe}: {exc!r}")
            await asyncio.sleep(interval)

    async def open_trade(
        self,
        *,
        symbol: str,
        side: str,
        volume: float,
        sl: float,
        tp: float,
    ) -> str:
        # open_market_order del broker real todavía no soporta SL/TP server-side.
        res = await self.broker.open_market_order(
            symbol=symbol,
            side=side,
            volume=volume,
            stop_loss=sl,
            take_profit=tp,
        )
        return str(res.get("position_id"))

    async def close_trade(self, position_id: int) -> None:
        await self.broker.close_position(position_id)

    async def price(self, symbol: str) -> float:
        return await get_current_price(symbol)
