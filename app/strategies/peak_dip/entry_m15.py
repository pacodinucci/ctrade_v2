from __future__ import annotations

import pandas as pd

from .utils import point_size, is_doji, sma, to_dataframe


def evaluate_entry(candles: list[dict], *, side: str, symbol: str, doji_points: int = 9) -> dict | None:
    df = to_dataframe(candles)
    if df.empty:
        return None
    pt = point_size(symbol)
    df["doji"] = (df["close"] - df["open"]).abs() < (doji_points * pt)
    df["sma8"] = sma(df["close"], 8)
    df = df.dropna(subset=["sma8"])
    if df.empty:
        return None
    body = (df["close"] - df["open"]).abs()
    if side == "sell":
        cross = (df["open"] > df["sma8"]) & (df["close"] < df["sma8"])
        lower_wick = df[["open", "close"]].min(axis=1) - df["low"]
        valid = cross & (~df["doji"]) & (lower_wick <= body)
    else:
        cross = (df["open"] < df["sma8"]) & (df["close"] > df["sma8"])
        upper_wick = df["high"] - df[["open", "close"]].max(axis=1)
        valid = cross & (~df["doji"]) & (upper_wick <= body)
    hit = df[valid]
    if hit.empty:
        return None
    row = hit.iloc[-1]
    return {"time": row["time_utc"], "entry": float(row["close"]), "sma8": float(row["sma8"])}
