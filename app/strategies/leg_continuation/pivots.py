from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

PivotType = Literal["high", "low"]


@dataclass(frozen=True)
class Pivot:
    time_utc: pd.Timestamp
    price: float
    pivot_type: PivotType


def find_pivots(candles: pd.DataFrame, strength: int = 2) -> pd.DataFrame:
    if candles.empty:
        return pd.DataFrame(columns=["time_utc", "price", "pivot_type"])
    if strength < 1:
        raise ValueError("pivot strength debe ser >= 1")

    d = candles.copy()
    d["time_utc"] = pd.to_datetime(d["time_utc"], utc=True)
    d = d.sort_values("time_utc").reset_index(drop=True)
    if len(d) < (2 * strength + 1):
        return pd.DataFrame(columns=["time_utc", "price", "pivot_type"])

    rows: list[dict] = []
    highs = d["high"].astype(float).tolist()
    lows = d["low"].astype(float).tolist()

    for i in range(strength, len(d) - strength):
        left_h = highs[i - strength : i]
        right_h = highs[i + 1 : i + 1 + strength]
        left_l = lows[i - strength : i]
        right_l = lows[i + 1 : i + 1 + strength]
        center_h = highs[i]
        center_l = lows[i]

        is_pivot_high = all(center_h > x for x in left_h) and all(center_h >= x for x in right_h)
        is_pivot_low = all(center_l < x for x in left_l) and all(center_l <= x for x in right_l)

        ts = pd.Timestamp(d.iloc[i]["time_utc"]).tz_convert("UTC")
        if is_pivot_high:
            rows.append({"time_utc": ts, "price": float(center_h), "pivot_type": "high"})
        if is_pivot_low:
            rows.append({"time_utc": ts, "price": float(center_l), "pivot_type": "low"})

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values("time_utc").reset_index(drop=True)


def compress_pivots(pivots: pd.DataFrame) -> pd.DataFrame:
    if pivots.empty:
        return pd.DataFrame(columns=["time_utc", "price", "pivot_type"])

    d = pivots.copy()
    d["time_utc"] = pd.to_datetime(d["time_utc"], utc=True)
    d = d.sort_values("time_utc").reset_index(drop=True)

    kept: list[dict] = []
    for row in d.itertuples(index=False):
        item = {"time_utc": pd.Timestamp(row.time_utc).tz_convert("UTC"), "price": float(row.price), "pivot_type": str(row.pivot_type)}
        if not kept:
            kept.append(item)
            continue

        prev = kept[-1]
        if item["pivot_type"] != prev["pivot_type"]:
            kept.append(item)
            continue

        if item["pivot_type"] == "high":
            if item["price"] >= prev["price"]:
                kept[-1] = item
        else:
            if item["price"] <= prev["price"]:
                kept[-1] = item

    out = pd.DataFrame(kept)
    if out.empty:
        return out
    return out.sort_values("time_utc").reset_index(drop=True)


def build_legs_extended(pivots: pd.DataFrame) -> pd.DataFrame:
    if pivots.empty or len(pivots) < 2:
        return pd.DataFrame(
            columns=["start_time", "end_time", "start_price", "end_price", "leg_type"]
        )

    d = pivots.copy()
    d["time_utc"] = pd.to_datetime(d["time_utc"], utc=True)
    d = d.sort_values("time_utc").reset_index(drop=True)

    rows: list[dict] = []
    for i in range(1, len(d)):
        p0 = d.iloc[i - 1]
        p1 = d.iloc[i]
        start_price = float(p0["price"])
        end_price = float(p1["price"])
        if end_price == start_price:
            continue
        rows.append(
            {
                "start_time": pd.Timestamp(p0["time_utc"]).tz_convert("UTC"),
                "end_time": pd.Timestamp(p1["time_utc"]).tz_convert("UTC"),
                "start_price": start_price,
                "end_price": end_price,
                "leg_type": "bullish" if end_price > start_price else "bearish",
            }
        )

    return pd.DataFrame(rows)
