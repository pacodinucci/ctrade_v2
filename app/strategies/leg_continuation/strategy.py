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
    breakout_close: float | None = None
    entry_retest_done: bool = False


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
        retest_tolerance_points: float = 10.0,
        rejection_wick_ratio: float = 1.5,
        trigger_invalidation_points: float = 200.0,
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
        self._retest_tolerance_points = float(retest_tolerance_points)
        self._rejection_wick_ratio = float(rejection_wick_ratio)
        self._trigger_invalidation_points = float(trigger_invalidation_points)
        if self._retest_tolerance_points < 0:
            raise ValueError("retest_tolerance_points debe ser >= 0")
        if self._rejection_wick_ratio <= 0:
            raise ValueError("rejection_wick_ratio debe ser > 0")
        if self._trigger_invalidation_points < 0:
            raise ValueError("trigger_invalidation_points debe ser >= 0")

        self._h4: Deque[dict[str, Any]] = deque(maxlen=500)
        self._m15: Deque[dict[str, Any]] = deque(maxlen=1200)
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
                    setup.breakout_close = close_px
            active.append(setup)
        self._pending = active

    async def on_m15_close(self, candle: dict) -> None:
        c = self._normalize_candle(candle)
        self._m15.append(c)
        now = ensure_utc_timestamp(candle["time_utc"])
        if not self._pending:
            return

        close_px = float(c["close"])
        open_px = float(c["open"])
        high_px = float(c["high"])
        low_px = float(c["low"])
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

            # Trigger en timeframe menor:
            # 1) usar el cierre de la vela MAYOR que confirmo breakout
            # 2) esperar retest de ese nivel de cierre
            # 3) esperar nuevo cierre a favor por encima/debajo de ese nivel
            if setup.breakout_close is None:
                active.append(setup)
                continue
            if self._is_trigger_search_invalidated(
                side=setup.side,
                close_px=close_px,
                continuation_level=setup.continuation_level,
            ):
                continue

            if not setup.entry_retest_done:
                if self._did_retest_close_level(
                    side=setup.side,
                    open_px=open_px,
                    close_px=close_px,
                    high_px=high_px,
                    low_px=low_px,
                    break_close=setup.continuation_level,
                ):
                    setup.entry_retest_done = True
                active.append(setup)
                continue

            if not self._is_level_break(side=setup.side, close_px=close_px, level=setup.breakout_close):
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
                "breakout_close": first.breakout_close,
            }

        payload["h4_last_4"] = list(self._h4)[-4:]
        payload["m15_last_4"] = list(self._m15)[-4:]
        return payload

    def _rebuild_pending_from_h4(self) -> None:
        h4 = pd.DataFrame(list(self._h4))
        h4["time_utc"] = pd.to_datetime(h4["time_utc"], utc=True)
        h4 = h4.sort_values("time_utc").reset_index(drop=True)
        current_h4_time = pd.Timestamp(h4["time_utc"].max()).tz_convert("UTC")

        pivots = compress_pivots(find_pivots(h4, strength=self.pivot_strength))
        legs = build_legs_extended(pivots)
        if legs.empty or len(legs) < 2:
            self._pending.clear()
            return

        # Nuevo enfoque: setup con AB confirmado, C en formacion.
        leg_a = legs.iloc[-2]
        leg_b = legs.iloc[-1]

        if leg_a["leg_type"] == "bullish" and leg_b["leg_type"] == "bearish":
            side = "buy"
        elif leg_a["leg_type"] == "bearish" and leg_b["leg_type"] == "bullish":
            side = "sell"
        else:
            self._pending.clear()
            return

        continuation_level = float(leg_a["end_price"])
        search_start = pd.Timestamp(leg_b["end_time"]).tz_convert("UTC")
        search_end = current_h4_time + pd.Timedelta(hours=4)

        setup_id = self._build_setup_id(
            side=side,
            continuation_level=continuation_level,
            leg_a_end_time=pd.Timestamp(leg_a["end_time"]).tz_convert("UTC"),
            leg_b_end_time=pd.Timestamp(leg_b["end_time"]).tz_convert("UTC"),
        )

        prev_breakout: pd.Timestamp | None = None
        if self._pending and self._pending[0].setup_id == setup_id:
            prev_breakout = self._pending[0].breakout_time

        breakout_time, breakout_close = self._resolve_breakout_from_h4_history(
            h4=h4,
            side=side,
            continuation_level=continuation_level,
            search_start=search_start,
            search_end=current_h4_time,
        )
        if breakout_time is None:
            breakout_time = prev_breakout
            if self._pending and self._pending[0].setup_id == setup_id:
                breakout_close = self._pending[0].breakout_close

        self._pending = deque(
            [
                PendingSetup(
                    setup_id=setup_id,
                    side=side,
                    continuation_level=continuation_level,
                    search_start=search_start,
                    search_end=search_end,
                    breakout_time=breakout_time,
                    breakout_close=breakout_close,
                )
            ]
        )

    def _resolve_breakout_from_h4_history(
        self,
        *,
        h4: pd.DataFrame,
        side: str,
        continuation_level: float,
        search_start: pd.Timestamp,
        search_end: pd.Timestamp,
    ) -> tuple[pd.Timestamp | None, float | None]:
        window = h4[
            (h4["time_utc"] > search_start)
            & (h4["time_utc"] <= search_end)
        ]
        if window.empty:
            return None, None

        for row in window.itertuples(index=False):
            if self._is_level_break(
                side=side,
                close_px=float(row.close),
                level=continuation_level,
            ):
                return ensure_utc_timestamp(row.time_utc), float(row.close)
        return None, None

    def _build_setup_id(
        self,
        *,
        side: str,
        continuation_level: float,
        leg_a_end_time: pd.Timestamp,
        leg_b_end_time: pd.Timestamp,
    ) -> str:
        return (
            f"{side}|{continuation_level:.8f}|"
            f"{ensure_utc_timestamp(leg_a_end_time).isoformat()}|"
            f"{ensure_utc_timestamp(leg_b_end_time).isoformat()}"
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

    def _is_trigger_search_invalidated(
        self,
        *,
        side: str,
        close_px: float,
        continuation_level: float,
    ) -> bool:
        invalidation_distance = self._trigger_invalidation_points * point_size(self.symbol)
        side_u = str(side).strip().lower()
        if side_u == "buy":
            return float(close_px) < (float(continuation_level) - invalidation_distance)
        return float(close_px) > (float(continuation_level) + invalidation_distance)

    @staticmethod
    def _candle_wicks(*, open_px: float, close_px: float, high_px: float, low_px: float) -> tuple[float, float, float]:
        body = abs(float(close_px) - float(open_px))
        upper_wick = float(high_px) - max(float(open_px), float(close_px))
        lower_wick = min(float(open_px), float(close_px)) - float(low_px)
        return max(body, 0.0), max(upper_wick, 0.0), max(lower_wick, 0.0)

    def _did_retest_close_level(
        self,
        *,
        side: str,
        open_px: float,
        close_px: float,
        high_px: float,
        low_px: float,
        break_close: float,
    ) -> bool:
        tol = self._retest_tolerance_points * point_size(self.symbol)
        zone_lo = float(break_close) - tol
        zone_hi = float(break_close) + tol

        # Retest valido: el rango de la vela visita la zona tolerada alrededor del nivel.
        zone_touched = float(low_px) <= zone_hi and float(high_px) >= zone_lo
        return zone_touched

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
