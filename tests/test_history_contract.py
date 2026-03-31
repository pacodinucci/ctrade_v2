from datetime import timezone

import pytest

from app.api.history_contract import (
    parse_datetime_param,
    parse_limit,
    normalize_timeframe,
    serialize_history_payload,
)


def test_normalize_timeframe_accepts_aliases() -> None:
    assert normalize_timeframe("m1") == "M1"
    assert normalize_timeframe("5m") == "M5"
    assert normalize_timeframe("MINUTE_15") == "M15"
    assert normalize_timeframe("1h") == "H1"
    assert normalize_timeframe("HOUR_4") == "H4"


def test_normalize_timeframe_rejects_invalid_value() -> None:
    with pytest.raises(ValueError, match="timeframe must be one of"):
        normalize_timeframe("M30")


def test_parse_limit_bounds() -> None:
    assert parse_limit(1) == 1
    assert parse_limit(2500) == 2500
    with pytest.raises(ValueError, match="limit must be between 1 and 2500"):
        parse_limit(0)
    with pytest.raises(ValueError, match="limit must be between 1 and 2500"):
        parse_limit(2501)


def test_parse_datetime_param_supports_iso_and_date() -> None:
    iso_start = parse_datetime_param("start", "2026-03-31T10:00:00Z", end_of_day_if_date=False)
    assert iso_start.isoformat() == "2026-03-31T10:00:00+00:00"

    date_start = parse_datetime_param("start", "2026-03-31", end_of_day_if_date=False)
    assert date_start.isoformat() == "2026-03-31T00:00:00+00:00"

    date_end = parse_datetime_param("end", "2026-03-31", end_of_day_if_date=True)
    assert date_end.isoformat() == "2026-03-31T23:59:59.999999+00:00"
    assert date_end.tzinfo == timezone.utc


def test_serialize_history_payload_shape() -> None:
    payload = serialize_history_payload(
        instrument="USDCHF",
        timeframe="M5",
        candles=[
            {
                "time": "2026-03-31T16:15:00Z",
                "open": 0.80231,
                "high": 0.80256,
                "low": 0.80227,
                "close": 0.80249,
            }
        ],
    )

    assert payload["instrument"] == "USDCHF"
    assert payload["timeframe"] == "M5"
    assert payload["count"] == 1
    assert isinstance(payload["candles"], list)
