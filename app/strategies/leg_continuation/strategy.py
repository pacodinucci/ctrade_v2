from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Deque

import pandas as pd

from app.services.ctrader_client import cTraderClient
from app.strategies.peak_dip.utils import point_size
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


class LegContinuationH4M15Strategy:
    name = "leg_continuation_h4_m15"
    required_timeframes = ("H4", "M15")

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

        self._h4: Deque[dict[str, Any]] = deque(maxlen=500)
        self._m15: Deque[dict[str, Any]] = deque(maxlen=1200)
        self._known_setup_ids: set[str] = set()
        self._pending: Deque[PendingSetup] = deque()

    async def on_h4_close(self, candle: dict) -> None:
        self._h4.append(self._normalize_candle(candle))
        if len(self._h4) < (2 * self.pivot_strength + 5):
            return

        self._rebuild_pending_from_h4()
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

    async def on_m15_close(self, candle: dict) -> None:
        self._m15.append(self._normalize_candle(candle))
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

            # Regla critica: maximo una posicion simultanea por bot/simbolo.
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
        stage = "WAITING_H4_LEGS"
        payload: dict[str, Any] = {
            "strategy": self.name,
            "symbol": self.symbol,
            "stage": stage,
            "h4_count": len(self._h4),
            "m15_count": len(self._m15),
            "pivot_strength": self.pivot_strength,
            "pending_setups_count": len(self._pending),
        }

        if self._pending:
            stage = "WAITING_BREAKOUT_OR_ENTRY"
            first = self._pending[0]
            payload["stage"] = stage
            payload["current_setup"] = {
                "side": first.side,
                "continuation_level": first.continuation_level,
                "search_start": first.search_start.isoformat(),
                "search_end": first.search_end.isoformat(),
                "breakout_time": first.breakout_time.isoformat() if first.breakout_time is not None else None,
            }

        payload["h4_last_4"] = list(self._h4)[-4:]
        payload["m15_last_4"] = list(self._m15)[-4:]
        return payload

    def _rebuild_pending_from_h4(self) -> None:
        h4 = pd.DataFrame(list(self._h4))
        h4["time_utc"] = pd.to_datetime(h4["time_utc"], utc=True)
        h4 = h4.sort_values("time_utc").reset_index(drop=True)

        pivots = compress_pivots(find_pivots(h4, strength=self.pivot_strength))
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
                search_end = pd.Timestamp(h4["time_utc"].max()).tz_convert("UTC") + pd.Timedelta(hours=4)

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

    def build_trade_plan(self, side: str, entry: float) -> dict[str, float | str]:
        pt = point_size(self.symbol)
        if side == "sell":
            sl = float(entry) + (self.sl_points * pt)
            tp = float(entry) - (self.tp_points * pt)
        else:
            sl = float(entry) - (self.sl_points * pt)
            tp = float(entry) + (self.tp_points * pt)
            side = "buy"
        return {"side": side, "entry": float(entry), "sl": sl, "tp": tp}
