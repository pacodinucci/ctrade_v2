from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from app.services.ctrader_client import cTraderClient


class FastTestStrategy:
    name = "fast_test"

    def __init__(
        self,
        *,
        symbol: str,
        client: cTraderClient,
        side: str = "buy",
        volume: float = 100000.0,
        sl_points: int = 30,
        tp_points: int = 30,
        open_interval_sec: float = 20.0,
        hold_sec: float = 8.0,
        max_cycles: int = 0,
    ) -> None:
        self.symbol = symbol
        self.client = client
        self.side = side.lower().strip()
        self.volume = float(volume)
        self.sl_points = int(sl_points)
        self.tp_points = int(tp_points)
        self.open_interval_sec = max(2.0, float(open_interval_sec))
        self.hold_sec = max(1.0, float(hold_sec))
        self.max_cycles = int(max_cycles)

        self._task: asyncio.Task | None = None
        self._running = False
        self._cycles_done = 0
        self._last_position_id: str | None = None
        self._last_error: str | None = None
        self._last_event: str | None = None
        self._last_event_time: str | None = None

    async def on_start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name=f"fast-test-{self.symbol}")

    async def shutdown(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None

    async def on_h4_close(self, candle: dict) -> None:
        return

    async def on_m15_close(self, candle: dict) -> None:
        return

    def get_runtime_state(self) -> dict[str, Any]:
        return {
            "strategy": self.name,
            "symbol": self.symbol,
            "stage": "RUNNING_CYCLE" if self._running else "IDLE",
            "side": self.side,
            "volume": self.volume,
            "open_interval_sec": self.open_interval_sec,
            "hold_sec": self.hold_sec,
            "max_cycles": self.max_cycles,
            "cycles_done": self._cycles_done,
            "last_position_id": self._last_position_id,
            "last_event": self._last_event,
            "last_event_time": self._last_event_time,
            "last_error": self._last_error,
        }

    async def _run_loop(self) -> None:
        while self._running:
            if self.max_cycles > 0 and self._cycles_done >= self.max_cycles:
                self._last_event = "MAX_CYCLES_REACHED"
                self._last_event_time = datetime.now(timezone.utc).isoformat()
                self._running = False
                break

            try:
                position_id = await self.client.open_trade(
                    symbol=self.symbol,
                    side=self.side,
                    volume=self.volume,
                    sl_points=self.sl_points,
                    tp_points=self.tp_points,
                )
                self._last_position_id = str(position_id)
                self._last_event = "OPENED"
                self._last_event_time = datetime.now(timezone.utc).isoformat()

                await asyncio.sleep(self.hold_sec)

                if self._last_position_id and self._last_position_id.isdigit():
                    await self.client.close_trade(int(self._last_position_id))
                    self._last_event = "CLOSED"
                    self._last_event_time = datetime.now(timezone.utc).isoformat()

                self._cycles_done += 1
                rest = max(0.0, self.open_interval_sec - self.hold_sec)
                if rest > 0:
                    await asyncio.sleep(rest)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                self._last_event = "ERROR"
                self._last_event_time = datetime.now(timezone.utc).isoformat()
                await asyncio.sleep(min(self.open_interval_sec, 5.0))
