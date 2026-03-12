from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple


@dataclass
class _CandleState:
    start: datetime
    open: float
    high: float
    low: float
    close: float


class BarBuilder:
    """
    Construye velas M1 desde ticks y deriva M5/M15 desde M1.
    Devuelve una lista de cierres: [(timeframe, candle_dict)].
    """

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol.upper()
        self._m1_current: _CandleState | None = None
        self._m5_current: _CandleState | None = None
        self._m15_current: _CandleState | None = None

    def on_tick(self, *, price: float, ts: datetime) -> List[Tuple[str, dict]]:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)

        closed: List[Tuple[str, dict]] = []
        minute_start = ts.replace(second=0, microsecond=0)

        if self._m1_current is None:
            self._m1_current = _CandleState(minute_start, price, price, price, price)
            return closed

        if minute_start == self._m1_current.start:
            self._update(self._m1_current, price)
            return closed

        # Cerramos M1 actual y, si hay huecos, completamos minutos faltantes flat.
        prev = self._m1_current
        closed_m1 = self._to_candle(prev)
        closed.append(("M1", closed_m1))
        closed.extend(self._feed_derived_from_m1(closed_m1))

        gap_start = prev.start + timedelta(minutes=1)
        while gap_start < minute_start:
            flat = {
                "symbol": self.symbol,
                "timeframe": "M1",
                "time_utc": gap_start.isoformat(),
                "open": prev.close,
                "high": prev.close,
                "low": prev.close,
                "close": prev.close,
            }
            closed.append(("M1", flat))
            closed.extend(self._feed_derived_from_m1(flat))
            prev = _CandleState(gap_start, prev.close, prev.close, prev.close, prev.close)
            gap_start = gap_start + timedelta(minutes=1)

        self._m1_current = _CandleState(minute_start, price, price, price, price)
        return closed

    def _feed_derived_from_m1(self, m1_candle: dict) -> List[Tuple[str, dict]]:
        out: List[Tuple[str, dict]] = []
        out.extend(self._update_higher_from_m1("M5", m1_candle))
        out.extend(self._update_higher_from_m1("M15", m1_candle))
        return out

    def _update_higher_from_m1(self, timeframe: str, m1_candle: dict) -> List[Tuple[str, dict]]:
        out: List[Tuple[str, dict]] = []

        start = datetime.fromisoformat(str(m1_candle["time_utc"]))
        price_open = float(m1_candle["open"])
        price_high = float(m1_candle["high"])
        price_low = float(m1_candle["low"])
        price_close = float(m1_candle["close"])

        if timeframe == "M5":
            bucket_minute = (start.minute // 5) * 5
            bucket = start.replace(minute=bucket_minute, second=0, microsecond=0)
            state = self._m5_current
        else:
            bucket_minute = (start.minute // 15) * 15
            bucket = start.replace(minute=bucket_minute, second=0, microsecond=0)
            state = self._m15_current

        if state is None:
            state = _CandleState(bucket, price_open, price_high, price_low, price_close)
            if timeframe == "M5":
                self._m5_current = state
            else:
                self._m15_current = state
            return out

        if bucket == state.start:
            state.high = max(state.high, price_high)
            state.low = min(state.low, price_low)
            state.close = price_close
            return out

        # Cierre del bucket anterior.
        out.append((timeframe, self._to_candle(state, timeframe=timeframe)))

        # Avance a nuevo bucket.
        state = _CandleState(bucket, price_open, price_high, price_low, price_close)
        if timeframe == "M5":
            self._m5_current = state
        else:
            self._m15_current = state

        return out

    @staticmethod
    def _update(state: _CandleState, price: float) -> None:
        state.high = max(state.high, price)
        state.low = min(state.low, price)
        state.close = price

    def _to_candle(self, state: _CandleState, timeframe: str = "M1") -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": timeframe,
            "time_utc": state.start.isoformat(),
            "open": state.open,
            "high": state.high,
            "low": state.low,
            "close": state.close,
        }
