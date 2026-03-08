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

    signals: list[dict] = []
    last_non_doji: list[tuple[pd.Timestamp, bool, bool, float]] = []
    for _, row in d.iterrows():
        if row["doji"]:
            continue
        last_non_doji.append(
            (row["time_utc"], bool(row["bull"]), bool(row["bear"]), float(row["close"]))
        )
        if len(last_non_doji) > 4:
            last_non_doji.pop(0)
        if len(last_non_doji) < 4:
            continue
        (t1, b1, r1, c1), (t2, b2, r2, c2), (t3, b3, r3, c3), (t4, b4, r4, c4) = last_non_doji
        if b1 and b2 and b3 and r4 and (c1 < c2 < c3):
            signals.append({"time": t4, "side": "sell"})
            last_non_doji = []
            continue
        if r1 and r2 and r3 and b4 and (c1 > c2 > c3):
            signals.append({"time": t4, "side": "buy"})
            last_non_doji = []
            continue
    return signals
