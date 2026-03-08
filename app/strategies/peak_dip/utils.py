from __future__ import annotations

from typing import Iterable
import pandas as pd


def point_size(symbol: str) -> float:
    if symbol.endswith("JPY"):
        return 0.001
    if symbol.upper() in ("XAUUSD", "XAGUSD"):
        return 0.01
    return 0.00001


def is_doji(candle: dict, doji_points: int, pt: float) -> bool:
    return abs(candle["close"] - candle["open"]) < (doji_points * pt)


def to_dataframe(candles: Iterable[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    if not df.empty:
        df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
        df = df.sort_values("time_utc").reset_index(drop=True)
    return df


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()
