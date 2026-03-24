from __future__ import annotations

from collections import deque
from typing import Any, Deque

import pandas as pd

from app.services.ctrader_client import cTraderClient
from app.utils.time import ensure_utc_timestamp
from app.strategies.peak_dip.detector_h4 import PeakDipDetector
from app.strategies.peak_dip.entry_m15 import evaluate_entry
from app.strategies.peak_dip.trade_manager import TradeManager
from app.strategies.peak_dip.filters import is_blocked_after_friday_cutoff
from app.strategies.peak_dip.utils import point_size, is_doji


class PeakDipM5M1Strategy:
    name = "peak_dip_m5_m1"
    required_timeframes = ("M5", "M1")

    def __init__(
        self,
        *,
        symbol: str,
        client: cTraderClient,
        volume: float = 100000.0,
        doji_points: int = 9,
        sl_points: int = 100,
        tp_points: int = 200,
        m5_valid_bars: int = 24,
        friday_cutoff_hour: int = 10,
        cutoff_tz: str = "UTC",
    ) -> None:
        self.symbol = symbol
        self.client = client
        self.volume = float(volume)
        self.doji_points = doji_points
        self.detector = PeakDipDetector(symbol, doji_points)
        self.trade_manager = TradeManager(symbol, sl_points, tp_points)
        self.m5_valid_minutes = 5 * m5_valid_bars
        self.friday_cutoff_hour = friday_cutoff_hour
        self.cutoff_tz = cutoff_tz

        self.pending_windows: Deque[dict] = deque()
        self.m1_buffer: Deque[dict] = deque(maxlen=180)
        self.m5_buffer: Deque[dict[str, Any]] = deque(maxlen=80)

    async def on_h4_close(self, candle: dict) -> None:
        return

    async def on_m15_close(self, candle: dict) -> None:
        return

    async def on_m5_close(self, candle: dict) -> None:
        self._remember_m5(candle)
        signals = self.detector.feed(candle)
        for sig in signals:
            setup_time = ensure_utc_timestamp(sig["time"])
            if is_blocked_after_friday_cutoff(
                setup_time,
                cutoff_hour=self.friday_cutoff_hour,
                tz=self.cutoff_tz,
            ):
                continue
            deadline = setup_time + pd.Timedelta(minutes=self.m5_valid_minutes)
            self.pending_windows.append(
                {
                    "side": sig["side"],
                    "setup_time": setup_time,
                    "deadline": deadline,
                }
            )

    async def on_m1_close(self, candle: dict) -> None:
        self.m1_buffer.append(candle)
        now = ensure_utc_timestamp(candle["time_utc"])
        if not self.pending_windows:
            return

        active = []
        opened = False
        while self.pending_windows:
            window = self.pending_windows.popleft()
            if now > window["deadline"]:
                continue

            entry = evaluate_entry(
                list(self.m1_buffer),
                side=window["side"],
                symbol=self.symbol,
                setup_time=window["setup_time"],
            )
            if not entry:
                active.append(window)
                continue

            # Regla critica: maximo una posicion simultanea por bot/simbolo.
            if await self.client.has_open_position(self.symbol):
                active.append(window)
                continue

            await self.client.open_trade(
                symbol=self.symbol,
                side=window["side"],
                volume=self.volume,
                sl_points=self.trade_manager.sl_points,
                tp_points=self.trade_manager.tp_points,
            )
            opened = True
            break

        if opened:
            self.pending_windows.clear()
            return

        self.pending_windows.extend(active)

    def get_runtime_state(self) -> dict[str, Any]:
        m5_snapshot = self._m5_snapshot()
        stage = "WAITING_M5_SETUP"
        payload: dict[str, Any] = {
            "strategy": self.name,
            "symbol": self.symbol,
            "stage": stage,
            "m5_last_4": m5_snapshot,
            "m5_progress": self._m5_progress(m5_snapshot),
            "pending_windows_count": len(self.pending_windows),
        }

        if self.pending_windows:
            current = self.pending_windows[0]
            payload["stage"] = "WAITING_M1_ENTRY"
            payload["current_window"] = {
                "side": current["side"],
                "setup_time": ensure_utc_timestamp(current["setup_time"]).isoformat(),
                "deadline": ensure_utc_timestamp(current["deadline"]).isoformat(),
            }
            payload["m1_buffer_size"] = len(self.m1_buffer)

        return payload

    def _remember_m5(self, candle: dict) -> None:
        ts = ensure_utc_timestamp(candle["time_utc"])
        self.m5_buffer.append(
            {
                "time_utc": ts,
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"]),
            }
        )

    def _m5_snapshot(self) -> list[dict[str, Any]]:
        pt = point_size(self.symbol)
        out: list[dict[str, Any]] = []
        for item in list(self.m5_buffer)[-4:]:
            c_open = float(item["open"])
            c_close = float(item["close"])
            doji = is_doji(item, self.doji_points, pt)
            direction = "doji"
            if not doji:
                direction = "bull" if c_close > c_open else "bear"
            out.append(
                {
                    "time_utc": ensure_utc_timestamp(item["time_utc"]).isoformat(),
                    "open": c_open,
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                    "close": c_close,
                    "direction": direction,
                    "is_doji": doji,
                }
            )
        return out

    def _m5_progress(self, m5_last_4: list[dict[str, Any]]) -> dict[str, Any]:
        non_doji = [c for c in m5_last_4 if not c["is_doji"]]
        progress: dict[str, Any] = {
            "non_doji_count": len(non_doji),
            "message": "Esperando 3 velas en un sentido y 1 reversa en M5",
        }

        if len(non_doji) < 3:
            progress["step"] = "COLLECTING_TREND"
            return progress

        last3 = non_doji[-3:]
        dirs3 = [c["direction"] for c in last3]
        if dirs3 == ["bull", "bull", "bull"]:
            progress["step"] = "WAITING_REVERSAL_BEAR"
            progress["candidate_side"] = "sell"
        elif dirs3 == ["bear", "bear", "bear"]:
            progress["step"] = "WAITING_REVERSAL_BULL"
            progress["candidate_side"] = "buy"
        else:
            progress["step"] = "RESET_COLLECTING"

        if len(non_doji) == 4:
            c1, c2, c3, c4 = non_doji
            sell_ready = (
                c1["direction"] == "bull"
                and c2["direction"] == "bull"
                and c3["direction"] == "bull"
                and c4["direction"] == "bear"
            )
            buy_ready = (
                c1["direction"] == "bear"
                and c2["direction"] == "bear"
                and c3["direction"] == "bear"
                and c4["direction"] == "bull"
            )
            if sell_ready or buy_ready:
                progress["step"] = "M5_SETUP_READY"
                progress["candidate_side"] = "sell" if sell_ready else "buy"

        return progress
