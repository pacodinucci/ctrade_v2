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
    breakout_level: float
    search_start: pd.Timestamp
    search_end: pd.Timestamp
    continuation_extended_at: pd.Timestamp | None = None
    breakout_confirmed_at: pd.Timestamp | None = None
    entry_retest_done: bool = False


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
        retest_tolerance_points: float = 10.0,
        rejection_wick_ratio: float = 1.5,
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
        if self._retest_tolerance_points < 0:
            raise ValueError("retest_tolerance_points debe ser >= 0")
        if self._rejection_wick_ratio <= 0:
            raise ValueError("rejection_wick_ratio debe ser > 0")

        self._m5: Deque[dict[str, Any]] = deque(maxlen=900)
        self._m1: Deque[dict[str, Any]] = deque(maxlen=2200)
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
            if now > setup.search_start:
                # Romper continuation_level no es breakout: solo confirma/expande continuidad.
                if self._is_level_break(side=setup.side, close_px=close_px, level=setup.continuation_level):
                    setup.continuation_extended_at = now

                # Breakout real: ruptura del extremo correctivo.
                if setup.breakout_confirmed_at is None and self._is_breakout_real(
                    side=setup.side,
                    close_px=close_px,
                    breakout_level=setup.breakout_level,
                ):
                    setup.breakout_confirmed_at = now
                    setup.entry_retest_done = False
            active.append(setup)
        self._pending = active

    async def on_m1_close(self, candle: dict) -> None:
        c = self._normalize_candle(candle)
        self._m1.append(c)
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
            if setup.breakout_confirmed_at is None:
                active.append(setup)
                continue
            if now <= setup.breakout_confirmed_at:
                active.append(setup)
                continue

            # Trigger en timeframe menor, habilitado solo tras breakout real en M5:
            # 2) esperar retest de ese nivel de cierre
            # 3) esperar nuevo cierre a favor por encima/debajo de ese nivel
            if not setup.entry_retest_done:
                if self._did_retest_close_level(
                    side=setup.side,
                    open_px=open_px,
                    close_px=close_px,
                    high_px=high_px,
                    low_px=low_px,
                    break_close=setup.breakout_level,
                ):
                    setup.entry_retest_done = True
                active.append(setup)
                continue

            if not self._is_breakout_real(
                side=setup.side,
                close_px=close_px,
                breakout_level=setup.breakout_level,
            ):
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
            if first.breakout_confirmed_at is not None:
                payload["stage"] = "WAITING_M1_ENTRY"
            else:
                payload["stage"] = "WAITING_M5_BREAKOUT"
            payload["current_setup"] = {
                "side": first.side,
                "continuation_level": first.continuation_level,
                "breakout_level": first.breakout_level,
                "search_start": first.search_start.isoformat(),
                "search_end": first.search_end.isoformat(),
                "continuation_extended_at": (
                    first.continuation_extended_at.isoformat()
                    if first.continuation_extended_at is not None
                    else None
                ),
                "breakout_confirmed_at": (
                    first.breakout_confirmed_at.isoformat()
                    if first.breakout_confirmed_at is not None
                    else None
                ),
                "entry_retest_done": bool(first.entry_retest_done),
            }

        payload["m5_last_4"] = list(self._m5)[-4:]
        payload["m1_last_4"] = list(self._m1)[-4:]
        return payload

    def _rebuild_pending_from_m5(self) -> None:
        m5 = pd.DataFrame(list(self._m5))
        m5["time_utc"] = pd.to_datetime(m5["time_utc"], utc=True)
        m5 = m5.sort_values("time_utc").reset_index(drop=True)
        current_m5_time = pd.Timestamp(m5["time_utc"].max()).tz_convert("UTC")

        pivots = compress_pivots(find_pivots(m5, strength=self.pivot_strength))
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
        breakout_level = self._resolve_breakout_level(side=side, corrective_leg=leg_b)
        search_start = pd.Timestamp(leg_b["end_time"]).tz_convert("UTC")
        search_end = current_m5_time + pd.Timedelta(minutes=5)

        setup_id = self._build_setup_id(
            side=side,
            continuation_level=continuation_level,
            breakout_level=breakout_level,
            leg_a_end_time=pd.Timestamp(leg_a["end_time"]).tz_convert("UTC"),
            leg_b_end_time=pd.Timestamp(leg_b["end_time"]).tz_convert("UTC"),
        )

        prev_continuation_extended_at: pd.Timestamp | None = None
        prev_breakout_confirmed_at: pd.Timestamp | None = None
        prev_entry_retest_done: bool = False
        if self._pending and self._pending[0].setup_id == setup_id:
            prev_continuation_extended_at = self._pending[0].continuation_extended_at
            prev_breakout_confirmed_at = self._pending[0].breakout_confirmed_at
            prev_entry_retest_done = bool(self._pending[0].entry_retest_done)

        continuation_extended_at = self._resolve_continuation_from_m5_history(
            m5=m5,
            side=side,
            continuation_level=continuation_level,
            search_start=search_start,
            search_end=current_m5_time,
        )
        if continuation_extended_at is None:
            continuation_extended_at = prev_continuation_extended_at

        breakout_confirmed_at = self._resolve_breakout_from_m5_history(
            m5=m5,
            side=side,
            breakout_level=breakout_level,
            search_start=search_start,
            search_end=current_m5_time,
        )
        if breakout_confirmed_at is None:
            breakout_confirmed_at = prev_breakout_confirmed_at

        self._pending = deque(
            [
                PendingSetup(
                    setup_id=setup_id,
                    side=side,
                    continuation_level=continuation_level,
                    breakout_level=breakout_level,
                    search_start=search_start,
                    search_end=search_end,
                    continuation_extended_at=continuation_extended_at,
                    breakout_confirmed_at=breakout_confirmed_at,
                    entry_retest_done=(
                        prev_entry_retest_done
                        if breakout_confirmed_at is not None
                        else False
                    ),
                )
            ]
        )

    def _resolve_continuation_from_m5_history(
        self,
        *,
        m5: pd.DataFrame,
        side: str,
        continuation_level: float,
        search_start: pd.Timestamp,
        search_end: pd.Timestamp,
    ) -> pd.Timestamp | None:
        window = m5[
            (m5["time_utc"] > search_start)
            & (m5["time_utc"] <= search_end)
        ]
        if window.empty:
            return None

        for row in window.itertuples(index=False):
            if self._is_level_break(
                side=side,
                close_px=float(row.close),
                level=continuation_level,
            ):
                return ensure_utc_timestamp(row.time_utc)
        return None

    def _resolve_breakout_from_m5_history(
        self,
        *,
        m5: pd.DataFrame,
        side: str,
        breakout_level: float,
        search_start: pd.Timestamp,
        search_end: pd.Timestamp,
    ) -> pd.Timestamp | None:
        window = m5[
            (m5["time_utc"] > search_start)
            & (m5["time_utc"] <= search_end)
        ]
        if window.empty:
            return None

        for row in window.itertuples(index=False):
            if self._is_breakout_real(
                side=side,
                close_px=float(row.close),
                breakout_level=breakout_level,
            ):
                return ensure_utc_timestamp(row.time_utc)
        return None

    def _build_setup_id(
        self,
        *,
        side: str,
        continuation_level: float,
        breakout_level: float,
        leg_a_end_time: pd.Timestamp,
        leg_b_end_time: pd.Timestamp,
    ) -> str:
        return (
            f"{side}|{continuation_level:.8f}|{breakout_level:.8f}|"
            f"{ensure_utc_timestamp(leg_a_end_time).isoformat()}|"
            f"{ensure_utc_timestamp(leg_b_end_time).isoformat()}"
        )

    @staticmethod
    def _resolve_breakout_level(*, side: str, corrective_leg: pd.Series) -> float:
        start_price = float(corrective_leg["start_price"])
        end_price = float(corrective_leg["end_price"])
        side_u = str(side).strip().lower()
        if side_u == "buy":
            return min(start_price, end_price)
        return max(start_price, end_price)

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

    @staticmethod
    def _is_breakout_real(*, side: str, close_px: float, breakout_level: float) -> bool:
        side_u = str(side).strip().lower()
        if side_u == "buy":
            return float(close_px) < float(breakout_level)
        return float(close_px) > float(breakout_level)

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
        side_u = str(side).strip().lower()
        tol = self._retest_tolerance_points * point_size(self.symbol)
        zone_lo = float(break_close) - tol
        zone_hi = float(break_close) + tol

        # Opcion 1: el rango de la vela visita la zona tolerada alrededor del nivel.
        zone_touched = float(low_px) <= zone_hi and float(high_px) >= zone_lo
        if zone_touched:
            return True

        # Opcion 2: vela de rechazo (cola larga) cerca de la zona, sin tocar exacto.
        body, upper_wick, lower_wick = self._candle_wicks(
            open_px=open_px,
            close_px=close_px,
            high_px=high_px,
            low_px=low_px,
        )
        body_ref = max(body, point_size(self.symbol))

        if side_u == "buy":
            near_zone = float(low_px) <= zone_hi
            return near_zone and (lower_wick / body_ref) >= self._rejection_wick_ratio

        near_zone = float(high_px) >= zone_lo
        return near_zone and (upper_wick / body_ref) >= self._rejection_wick_ratio
