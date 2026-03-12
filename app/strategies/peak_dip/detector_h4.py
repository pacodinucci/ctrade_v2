from __future__ import annotations

from typing import List
import pandas as pd

from .utils import point_size, is_doji, to_dataframe


class PeakDipDetector:
    def __init__(self, symbol: str, doji_points: int = 9) -> None:
        self.symbol = symbol
        self.doji_points = doji_points
        self._buffer: list[dict] = []

    def feed(self, candle: dict) -> list[dict]:
        self._buffer.append(candle)
        df = to_dataframe(self._buffer)
        signals = find_h4_turns(df, self.symbol, self.doji_points)
        # conservamos sólo últimas 10 velas para no crecer infinito
        self._buffer = self._buffer[-10:]
        return signals


def find_h4_turns(h4: pd.DataFrame, symbol: str, doji_points: int = 9) -> List[dict]:
    if h4.empty:
        return []

    pt = point_size(symbol)
    d = h4.copy()
    d["doji"] = (d["close"] - d["open"]).abs() < (doji_points * pt)
    d["bull"] = (d["close"] > d["open"]) & (~d["doji"])
    d["bear"] = (d["close"] < d["open"]) & (~d["doji"])

    non_doji = d[~d["doji"]].copy()
    if len(non_doji) < 4:
        return []

    last4 = non_doji.tail(4)
    rows = list(last4.itertuples(index=False))
    c1, c2, c3, c4 = rows

    first3_all_bull = bool(c1.bull and c2.bull and c3.bull)
    first3_all_bear = bool(c1.bear and c2.bear and c3.bear)

    if first3_all_bull and bool(c4.bear):
        return [{"time": c4.time_utc, "side": "sell"}]

    if first3_all_bear and bool(c4.bull):
        return [{"time": c4.time_utc, "side": "buy"}]

    return []
