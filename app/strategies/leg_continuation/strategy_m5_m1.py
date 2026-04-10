from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Deque

import pandas as pd

from app.services.ctrader_client import cTraderClient
from app.utils.time import ensure_utc_timestamp
from .pivots import build_legs_extended, compress_pivots, find_pivots


@dataclass
class PendingSetup:
    setup_id: str
    side: str
    continuation_level: float
    search_start: pd.Timestamp
    search_end: pd.Timestamp
    breakout_time: pd.Timestamp | None = None


class LegContinuationM5M1Strategy:
    name = "leg_continuation_m5_m1"
    required_timeframes = ("M5", "M1")

    def __init__(
        self,
        *,
        symbol: str,
        client: cTraderClient,
        volume: float = 100000.0,
        pivot_strength: int = 2,
        leg_mode: str = "extended",
        sl_points: int = 100,
        tp_points: int = 200,
    ) -> None:
        if int(pivot_strength) < 1:
            raise ValueError("pivot_strength debe ser >= 1")
        if str(leg_mode).strip().lower() != "extended":
            raise ValueError("solo leg_mode='extended' esta soportado")

        self.symbol = symbol
        self.client = client
        self.volume = float(volume)
        self.pivot_strength = int(pivot_strength)
        self.leg_mode = "extended"
        self.sl_points = int(sl_points)
        self.tp_points = int(tp_points)

        self._m5: Deque[dict[str, Any]] = deque(maxlen=900)
        self._m1: Deque[dict[str, Any]] = deque(maxlen=2200)
        self._known_setup_ids: set[str] = set()
        self._pending: Deque[PendingSetup] = deque()

    async def on_m5_close(self, candle: dict) -> None:
        self._m5.append(self._normalize_candle(candle))
        if len(self._m5) < (2 * self.pivot_strength + 5):
            return

        self._rebuild_pending_from_m5()
        now = ensure_utc_timestamp(candle["time_utc"])
        close_px = float(candle["close"])

        active: Deque[PendingSetup] = deque()
        while self._pending:
            setup = self._pending.popleft()
            if now > setup.search_end:
                continue
            if setup.breakout_time is None and now > setup.search_start:
                if self._is_level_break(side=setup.side, close_px=close_px, level=setup.continuation_level):
                    setup.breakout_time = now
            active.append(setup)
        self._pending = active

    async def on_m1_close(self, candle: dict) -> None:
        self._m1.append(self._normalize_candle(candle))
        now = ensure_utc_timestamp(candle["time_utc"])
        if not self._pending:
            return

        close_px = float(candle["close"])
        active: Deque[PendingSetup] = deque()
        opened = False

        while self._pending:
            setup = self._pending.popleft()
            if now > setup.search_end:
                continue
            if setup.breakout_time is None:
                active.append(setup)
                continue
            if now <= setup.breakout_time:
                active.append(setup)
                continue
            if not self._is_level_break(side=setup.side, close_px=close_px, level=setup.continuation_level):
                active.append(setup)
                continue

            if await self.client.has_open_position(self.symbol):
                active.append(setup)
                continue

            await self.client.open_trade(
                symbol=self.symbol,
                side=setup.side,
                volume=self.volume,
                bot_id=getattr(self, "bot_id", None),
                strategy=getattr(self, "strategy_id", self.name),
                source="bot",
                sl_points=self.sl_points,
                tp_points=self.tp_points,
            )
            opened = True
            break

        if opened:
            self._pending.clear()
            return
        self._pending = active

    def get_runtime_state(self) -> dict[str, Any]:
        stage = "WAITING_M5_LEGS"
        payload: dict[str, Any] = {
            "strategy": self.name,
            "symbol": self.symbol,
            "stage": stage,
            "m5_count": len(self._m5),
            "m1_count": len(self._m1),
            "pivot_strength": self.pivot_strength,
            "pending_setups_count": len(self._pending),
        }

        if self._pending:
            first = self._pending[0]
            payload["stage"] = "WAITING_BREAKOUT_OR_ENTRY"
            payload["current_setup"] = {
                "side": first.side,
                "continuation_level": first.continuation_level,
                "search_start": first.search_start.isoformat(),
                "search_end": first.search_end.isoformat(),
                "breakout_time": first.breakout_time.isoformat() if first.breakout_time is not None else None,
            }

        payload["m5_last_4"] = list(self._m5)[-4:]
        payload["m1_last_4"] = list(self._m1)[-4:]
        return payload

    def _rebuild_pending_from_m5(self) -> None:
        m5 = pd.DataFrame(list(self._m5))
        m5["time_utc"] = pd.to_datetime(m5["time_utc"], utc=True)
        m5 = m5.sort_values("time_utc").reset_index(drop=True)

        pivots = compress_pivots(find_pivots(m5, strength=self.pivot_strength))
        legs = build_legs_extended(pivots)
        if legs.empty or len(legs) < 3:
            return

        setups: list[PendingSetup] = []
        for i in range(2, len(legs)):
            leg_a = legs.iloc[i - 2]
            leg_b = legs.iloc[i - 1]
            leg_c = legs.iloc[i]

            if leg_a["leg_type"] == "bullish" and leg_b["leg_type"] == "bearish" and leg_c["leg_type"] == "bullish":
                side = "buy"
            elif leg_a["leg_type"] == "bearish" and leg_b["leg_type"] == "bullish" and leg_c["leg_type"] == "bearish":
                side = "sell"
            else:
                continue

            continuation_level = float(leg_a["end_price"])
            search_start = pd.Timestamp(leg_c["start_time"]).tz_convert("UTC")
            if i + 1 < len(legs):
                search_end = pd.Timestamp(legs.iloc[i + 1]["start_time"]).tz_convert("UTC")
            else:
                search_end = pd.Timestamp(m5["time_utc"].max()).tz_convert("UTC") + pd.Timedelta(minutes=5)

            setup_id = self._build_setup_id(
                side=side,
                continuation_level=continuation_level,
                leg_a_end_time=pd.Timestamp(leg_a["end_time"]).tz_convert("UTC"),
                leg_b_end_time=pd.Timestamp(leg_b["end_time"]).tz_convert("UTC"),
                leg_c_start_time=pd.Timestamp(leg_c["start_time"]).tz_convert("UTC"),
            )
            if setup_id in self._known_setup_ids:
                continue

            self._known_setup_ids.add(setup_id)
            setups.append(
                PendingSetup(
                    setup_id=setup_id,
                    side=side,
                    continuation_level=continuation_level,
                    search_start=search_start,
                    search_end=search_end,
                )
            )

        if not setups:
            return
        setups.sort(key=lambda x: x.search_start)
        self._pending.extend(setups)

    def _build_setup_id(
        self,
        *,
        side: str,
        continuation_level: float,
        leg_a_end_time: pd.Timestamp,
        leg_b_end_time: pd.Timestamp,
        leg_c_start_time: pd.Timestamp,
    ) -> str:
        return (
            f"{side}|{continuation_level:.8f}|"
            f"{ensure_utc_timestamp(leg_a_end_time).isoformat()}|"
            f"{ensure_utc_timestamp(leg_b_end_time).isoformat()}|"
            f"{ensure_utc_timestamp(leg_c_start_time).isoformat()}"
        )

    @staticmethod
    def _normalize_candle(candle: dict[str, Any]) -> dict[str, Any]:
        return {
            "time_utc": ensure_utc_timestamp(candle["time_utc"]),
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
        }

    @staticmethod
    def _is_level_break(*, side: str, close_px: float, level: float) -> bool:
        side_u = str(side).strip().lower()
        if side_u == "buy":
            return float(close_px) > float(level)
        return float(close_px) < float(level)
