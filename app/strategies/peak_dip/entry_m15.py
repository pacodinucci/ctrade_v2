from __future__ import annotations

import pandas as pd

from .utils import point_size, is_doji, sma, to_dataframe


def evaluate_entry(
    candles: list[dict],
    *,
    side: str,
    symbol: str,
    doji_points: int = 9,
    setup_time: pd.Timestamp | None = None,
    max_tail_body_ratio: float = 0.5,
) -> dict | None:
    df = to_dataframe(candles)
    if df.empty:
        return None
    side_u = str(side).strip().lower()
    if side_u not in ("buy", "sell"):
        return None

    if setup_time is not None:
        setup_ts = pd.to_datetime(setup_time, utc=True)
        df = df[df["time_utc"] >= setup_ts]
        if df.empty:
            return None

    pt = point_size(symbol)
    df["doji"] = (df["close"] - df["open"]).abs() < (doji_points * pt)
    df["sma8"] = sma(df["close"], 8)
    df = df.dropna(subset=["sma8"])
    if df.empty:
        return None

    # Trigger estricto: solo la ultima vela cerrada.
    row = df.iloc[-1]
    body = abs(float(row["close"]) - float(row["open"]))
    if body <= 0:
        return None

    open_px = float(row["open"])
    close_px = float(row["close"])
    sma8 = float(row["sma8"])
    high_px = float(row["high"])
    low_px = float(row["low"])
    is_doji_row = bool(row["doji"])

    if side_u == "sell":
        direction_ok = close_px < open_px
        cross_ok = open_px > sma8 and close_px < sma8
        tail = min(open_px, close_px) - low_px
    else:
        direction_ok = close_px > open_px
        cross_ok = open_px < sma8 and close_px > sma8
        tail = high_px - max(open_px, close_px)

    tail_ratio = tail / body if body > 0 else 999.0
    no_significant_tail = tail_ratio <= float(max_tail_body_ratio)
    valid = (not is_doji_row) and direction_ok and cross_ok and no_significant_tail
    if not valid:
        return None

    return {"time": row["time_utc"], "entry": close_px, "sma8": sma8}
