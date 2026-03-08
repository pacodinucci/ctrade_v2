from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .utils import point_size

Side = Literal["buy", "sell"]


@dataclass
class TradePlan:
    side: Side
    entry: float
    sl: float
    tp: float


class TradeManager:
    def __init__(self, symbol: str, sl_points: int = 100, tp_points: int = 200) -> None:
        self.symbol = symbol
        self.sl_points = sl_points
        self.tp_points = tp_points

    def build_plan(self, side: Side, entry: float) -> TradePlan:
        pt = point_size(self.symbol)
        sl_dist = self.sl_points * pt
        tp_dist = self.tp_points * pt
        if side == "sell":
            sl = entry + sl_dist
            tp = entry - tp_dist
        else:
            sl = entry - sl_dist
            tp = entry + tp_dist
        return TradePlan(side=side, entry=entry, sl=sl, tp=tp)
