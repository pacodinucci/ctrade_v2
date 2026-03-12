from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.services.ctrader_client import cTraderClient
from app.strategies.fast_test.strategy import FastTestStrategy
from app.strategies.peak_dip.strategy import PeakDipStrategy
from app.strategies.peak_dip_m5_m1 import PeakDipM5M1Strategy
from app.strategies.peak_dip.trade_manager import TradeManager
from app.strategies.peak_dip.utils import point_size


@dataclass(frozen=True)
class StrategyParamDef:
    key: str
    type: str
    required: bool
    default: Any


@dataclass(frozen=True)
class StrategyDefinition:
    id: str
    name: str
    params: tuple[StrategyParamDef, ...]
    normalize_params: Callable[[dict[str, Any] | None], dict[str, Any]]
    create_runtime: Callable[[str, cTraderClient, dict[str, Any]], Any]
    build_plan: Callable[[str, dict[str, Any], str, float], dict[str, Any]]


def _normalize_peak_dip_params(strategy_params: dict[str, Any] | None) -> dict[str, Any]:
    params = strategy_params or {}
    if not isinstance(params, dict):
        raise ValueError("strategyParams debe ser un objeto JSON")

    if params.get("volume") is None:
        raise ValueError("volume es requerido")
    volume = float(params.get("volume"))
    sl_points = int(params.get("sl_points", 100))
    tp_points = int(params.get("tp_points", 200))
    if volume < 100000:
        raise ValueError("volume debe ser >= 100000")
    if sl_points <= 0 or tp_points <= 0:
        raise ValueError("sl_points y tp_points deben ser mayores a 0")
    return {"volume": volume, "sl_points": sl_points, "tp_points": tp_points}


def _create_peak_dip_runtime(symbol: str, client: cTraderClient, params: dict[str, Any]) -> PeakDipStrategy:
    return PeakDipStrategy(
        symbol=symbol,
        client=client,
        volume=float(params["volume"]),
        sl_points=int(params["sl_points"]),
        tp_points=int(params["tp_points"]),
    )


def _build_peak_dip_plan(symbol: str, params: dict[str, Any], side: str, entry: float) -> dict[str, Any]:
    planner = TradeManager(
        symbol=symbol,
        sl_points=int(params["sl_points"]),
        tp_points=int(params["tp_points"]),
    )
    plan = planner.build_plan(side, entry)
    return {"side": plan.side, "entry": plan.entry, "sl": plan.sl, "tp": plan.tp}

def _normalize_peak_dip_m5_m1_params(strategy_params: dict[str, Any] | None) -> dict[str, Any]:
    return _normalize_peak_dip_params(strategy_params)


def _create_peak_dip_m5_m1_runtime(symbol: str, client: cTraderClient, params: dict[str, Any]) -> PeakDipM5M1Strategy:
    return PeakDipM5M1Strategy(
        symbol=symbol,
        client=client,
        volume=float(params["volume"]),
        sl_points=int(params["sl_points"]),
        tp_points=int(params["tp_points"]),
    )


def _build_peak_dip_m5_m1_plan(symbol: str, params: dict[str, Any], side: str, entry: float) -> dict[str, Any]:
    return _build_peak_dip_plan(symbol, params, side, entry)


def _normalize_fast_test_params(strategy_params: dict[str, Any] | None) -> dict[str, Any]:
    params = strategy_params or {}
    if not isinstance(params, dict):
        raise ValueError("strategyParams debe ser un objeto JSON")

    side = str(params.get("side", "buy")).strip().lower()
    if side not in ("buy", "sell"):
        raise ValueError("side debe ser buy o sell")

    if params.get("volume") is None:
        raise ValueError("volume es requerido")
    volume = float(params.get("volume"))
    sl_points = int(params.get("sl_points", 30))
    tp_points = int(params.get("tp_points", 30))
    open_interval_sec = float(params.get("open_interval_sec", 20))
    hold_sec = float(params.get("hold_sec", 8))
    max_cycles = int(params.get("max_cycles", 0))

    if volume < 100000:
        raise ValueError("volume debe ser >= 100000")
    if sl_points <= 0 or tp_points <= 0:
        raise ValueError("sl_points y tp_points deben ser mayores a 0")
    if open_interval_sec < 2:
        raise ValueError("open_interval_sec debe ser >= 2")
    if hold_sec < 1:
        raise ValueError("hold_sec debe ser >= 1")
    if max_cycles < 0:
        raise ValueError("max_cycles no puede ser negativo")

    return {
        "side": side,
        "volume": volume,
        "sl_points": sl_points,
        "tp_points": tp_points,
        "open_interval_sec": open_interval_sec,
        "hold_sec": hold_sec,
        "max_cycles": max_cycles,
    }


def _create_fast_test_runtime(symbol: str, client: cTraderClient, params: dict[str, Any]) -> FastTestStrategy:
    return FastTestStrategy(
        symbol=symbol,
        client=client,
        side=str(params["side"]),
        volume=float(params["volume"]),
        sl_points=int(params["sl_points"]),
        tp_points=int(params["tp_points"]),
        open_interval_sec=float(params["open_interval_sec"]),
        hold_sec=float(params["hold_sec"]),
        max_cycles=int(params["max_cycles"]),
    )


def _build_fast_test_plan(symbol: str, params: dict[str, Any], side: str, entry: float) -> dict[str, Any]:
    pt = point_size(symbol)
    sl_points = int(params["sl_points"])
    tp_points = int(params["tp_points"])

    resolved_side = str(params.get("side") or side).strip().lower()
    if resolved_side == "sell":
        sl = entry + (sl_points * pt)
        tp = entry - (tp_points * pt)
    else:
        resolved_side = "buy"
        sl = entry - (sl_points * pt)
        tp = entry + (tp_points * pt)

    return {"side": resolved_side, "entry": entry, "sl": sl, "tp": tp}


_REGISTRY: dict[str, StrategyDefinition] = {
    "peak_dip": StrategyDefinition(
        id="peak_dip",
        name="Peak/Dip",
        params=(
            StrategyParamDef(key="volume", type="float", required=True, default=100000),
            StrategyParamDef(key="sl_points", type="int", required=True, default=100),
            StrategyParamDef(key="tp_points", type="int", required=True, default=200),
        ),
        normalize_params=_normalize_peak_dip_params,
        create_runtime=_create_peak_dip_runtime,
        build_plan=_build_peak_dip_plan,
    ),
    "peak_dip_m5_m1": StrategyDefinition(
        id="peak_dip_m5_m1",
        name="Peak/Dip M5/M1",
        params=(
            StrategyParamDef(key="volume", type="float", required=True, default=100000),
            StrategyParamDef(key="sl_points", type="int", required=True, default=100),
            StrategyParamDef(key="tp_points", type="int", required=True, default=200),
        ),
        normalize_params=_normalize_peak_dip_m5_m1_params,
        create_runtime=_create_peak_dip_m5_m1_runtime,
        build_plan=_build_peak_dip_m5_m1_plan,
    ),
    "fast_test": StrategyDefinition(
        id="fast_test",
        name="Fast Test",
        params=(
            StrategyParamDef(key="side", type="string", required=False, default="buy"),
            StrategyParamDef(key="volume", type="float", required=True, default=100000),
            StrategyParamDef(key="sl_points", type="int", required=False, default=30),
            StrategyParamDef(key="tp_points", type="int", required=False, default=30),
            StrategyParamDef(key="open_interval_sec", type="float", required=False, default=20),
            StrategyParamDef(key="hold_sec", type="float", required=False, default=8),
            StrategyParamDef(key="max_cycles", type="int", required=False, default=0),
        ),
        normalize_params=_normalize_fast_test_params,
        create_runtime=_create_fast_test_runtime,
        build_plan=_build_fast_test_plan,
    ),
}


def get_strategy_definition(strategy_id: str) -> StrategyDefinition:
    try:
        return _REGISTRY[strategy_id]
    except KeyError as exc:
        raise ValueError(f"estrategia no soportada: {strategy_id}") from exc


def list_strategies_metadata() -> list[dict[str, Any]]:
    response: list[dict[str, Any]] = []
    for definition in _REGISTRY.values():
        response.append(
            {
                "id": definition.id,
                "name": definition.name,
                "params": [
                    {
                        "key": p.key,
                        "type": p.type,
                        "required": p.required,
                        "default": p.default,
                    }
                    for p in definition.params
                ],
            }
        )
    return response


def strategy_ids() -> tuple[str, ...]:
    return tuple(_REGISTRY.keys())
