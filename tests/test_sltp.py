from __future__ import annotations

import pytest

from app.services.sltp import (
    compute_sl_tp_from_points,
    is_retryable_ctrader_error,
    normalize_points,
    validate_sl_tp_for_side,
)


def test_normalize_points_uses_abs() -> None:
    assert normalize_points(-2.0) == 2.0
    assert normalize_points(3) == 3.0


def test_compute_sl_tp_buy() -> None:
    sl, tp = compute_sl_tp_from_points(
        side="buy",
        entry_price=1.1000,
        pip_size=0.0001,
        sl_points=20,
        tp_points=10,
    )
    assert sl == pytest.approx(1.0980)
    assert tp == pytest.approx(1.1010)


def test_compute_sl_tp_sell() -> None:
    sl, tp = compute_sl_tp_from_points(
        side="sell",
        entry_price=1.1000,
        pip_size=0.0001,
        sl_points=-20,
        tp_points=-10,
    )
    assert sl == pytest.approx(1.1020)
    assert tp == pytest.approx(1.0990)


def test_validate_sl_tp_for_side_rejects_bad_buy_levels() -> None:
    with pytest.raises(ValueError):
        validate_sl_tp_for_side(
            side="buy",
            entry_price=1.1000,
            stop_loss=1.1010,
            take_profit=1.0990,
            min_distance=0.0001,
        )


def test_retryability_classification_marks_bad_stops_non_retryable() -> None:
    assert is_retryable_ctrader_error("TRADING_BAD_STOPS") is False
    assert is_retryable_ctrader_error("REQUEST_TIMEOUT") is True
