from __future__ import annotations

from dataclasses import dataclass


NON_RETRYABLE_CTRADER_ERROR_CODES = {
    "TRADING_BAD_STOPS",
    "TRADING_BAD_PRICES",
    "TRADING_INVALID_REQUEST",
    "INVALID_REQUEST",
}


@dataclass
class CTraderOrderError(RuntimeError):
    error_code: str
    description: str
    position_id: int | None = None
    raw_request: dict | None = None

    def __post_init__(self) -> None:
        super().__init__(f"{self.error_code}: {self.description}")

    @property
    def retryable(self) -> bool:
        return is_retryable_ctrader_error(self.error_code)


def is_retryable_ctrader_error(error_code: str | None) -> bool:
    code = (error_code or "").strip().upper()
    if not code:
        return True
    return code not in NON_RETRYABLE_CTRADER_ERROR_CODES


def normalize_points(value: float | int | None) -> float | None:
    if value is None:
        return None
    return abs(float(value))


def compute_sl_tp_from_points(
    *,
    side: str,
    entry_price: float,
    pip_size: float,
    sl_points: float,
    tp_points: float,
) -> tuple[float, float]:
    side_u = side.strip().lower()
    if side_u not in {"buy", "sell"}:
        raise ValueError(f"Lado no soportado para SL/TP: {side}")
    if entry_price <= 0:
        raise ValueError("entry_price debe ser mayor a 0 para calcular SL/TP")
    if pip_size <= 0:
        raise ValueError("pip_size debe ser mayor a 0 para calcular SL/TP")

    sl_dist = normalize_points(sl_points) or 0.0
    tp_dist = normalize_points(tp_points) or 0.0
    if sl_dist <= 0 or tp_dist <= 0:
        raise ValueError("sl_points y tp_points deben ser mayores a 0")

    if side_u == "buy":
        return (
            float(entry_price) - (sl_dist * float(pip_size)),
            float(entry_price) + (tp_dist * float(pip_size)),
        )
    return (
        float(entry_price) + (sl_dist * float(pip_size)),
        float(entry_price) - (tp_dist * float(pip_size)),
    )


def validate_sl_tp_for_side(
    *,
    side: str,
    entry_price: float,
    stop_loss: float | None,
    take_profit: float | None,
    min_distance: float,
) -> None:
    side_u = side.strip().lower()
    if side_u not in {"buy", "sell"}:
        raise ValueError(f"Lado no soportado para validacion SL/TP: {side}")
    if entry_price <= 0:
        raise ValueError("entry_price debe ser mayor a 0")
    if min_distance <= 0:
        raise ValueError("min_distance debe ser mayor a 0")

    sl_value = float(stop_loss) if stop_loss is not None else None
    tp_value = float(take_profit) if take_profit is not None else None

    if sl_value is not None and sl_value <= 0:
        raise ValueError("stop_loss debe ser mayor a 0")
    if tp_value is not None and tp_value <= 0:
        raise ValueError("take_profit debe ser mayor a 0")

    if side_u == "buy":
        if sl_value is not None and sl_value >= entry_price:
            raise ValueError("SL invalido para BUY: debe ser menor al entry")
        if tp_value is not None and tp_value <= entry_price:
            raise ValueError("TP invalido para BUY: debe ser mayor al entry")
    else:
        if sl_value is not None and sl_value <= entry_price:
            raise ValueError("SL invalido para SELL: debe ser mayor al entry")
        if tp_value is not None and tp_value >= entry_price:
            raise ValueError("TP invalido para SELL: debe ser menor al entry")

    if sl_value is not None and abs(entry_price - sl_value) < min_distance:
        raise ValueError("Distancia de SL demasiado chica para el simbolo")
    if tp_value is not None and abs(entry_price - tp_value) < min_distance:
        raise ValueError("Distancia de TP demasiado chica para el simbolo")
