from __future__ import annotations

import asyncio
from collections import deque
from datetime import timedelta
from typing import Deque
import pandas as pd

from app.services.ctrader_client import cTraderClient
from .detector_h4 import PeakDipDetector
from .entry_m15 import evaluate_entry
from .trade_manager import TradeManager
from .filters import is_blocked_after_friday_cutoff


class PeakDipStrategy:
    name = "peak_dip"

    def __init__(
        self,
        *,
        symbol: str,
        client: cTraderClient,
        doji_points: int = 9,
        sl_points: int = 100,
        tp_points: int = 200,
        h4_valid_bars: int = 2,
        friday_cutoff_hour: int = 10,
        cutoff_tz: str = "UTC",
    ) -> None:
        self.symbol = symbol
        self.client = client
        self.detector = PeakDipDetector(symbol, doji_points)
        self.trade_manager = TradeManager(symbol, sl_points, tp_points)
        self.h4_valid_hours = 4 * h4_valid_bars
        self.friday_cutoff_hour = friday_cutoff_hour
        self.cutoff_tz = cutoff_tz
        self.pending_windows: Deque[dict] = deque()
        self.m15_buffer: Deque[dict] = deque(maxlen=64)

    async def on_h4_close(self, candle: dict) -> None:
        signals = self.detector.feed(candle)
        for sig in signals:
            setup_time = pd.Timestamp(sig["time"]).tz_convert("UTC")
            if is_blocked_after_friday_cutoff(
                setup_time,
                cutoff_hour=self.friday_cutoff_hour,
                tz=self.cutoff_tz,
            ):
                continue
            deadline = setup_time + pd.Timedelta(hours=self.h4_valid_hours)
            self.pending_windows.append(
                {
                    "side": sig["side"],
                    "setup_time": setup_time,
                    "deadline": deadline,
                }
            )

    async def on_m15_close(self, candle: dict) -> None:
        self.m15_buffer.append(candle)
        now = pd.Timestamp(candle["time_utc"]).tz_convert("UTC")
        if not self.pending_windows:
            return
        active = []
        while self.pending_windows:
            window = self.pending_windows.popleft()
            if now > window["deadline"]:
                continue
            active.append(window)
            entry = evaluate_entry(list(self.m15_buffer), side=window["side"], symbol=self.symbol)
            if entry:
                plan = self.trade_manager.build_plan(window["side"], entry["entry"])
                await self.client.open_trade(
                    symbol=self.symbol,
                    side=plan.side,
                    volume=0.01,
                    sl=plan.sl,
                    tp=plan.tp,
                )
                continue
        self.pending_windows.extend(active)
