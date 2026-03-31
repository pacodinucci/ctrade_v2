from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any

CANONICAL_TIMEFRAMES: tuple[str, ...] = ("M1", "M5", "M15", "H1", "H4")
MAX_LIMIT = 2500
DEFAULT_LIMIT = 500

_TIMEFRAME_ALIASES: dict[str, str] = {
    "M1": "M1",
    "1M": "M1",
    "M01": "M1",
    "MINUTE_1": "M1",
    "1MIN": "M1",
    "1MINUTE": "M1",
    "M5": "M5",
    "5M": "M5",
    "MINUTE_5": "M5",
    "5MIN": "M5",
    "5MINUTE": "M5",
    "M15": "M15",
    "15M": "M15",
    "MINUTE_15": "M15",
    "15MIN": "M15",
    "15MINUTE": "M15",
    "H1": "H1",
    "1H": "H1",
    "HOUR_1": "H1",
    "60M": "H1",
    "H4": "H4",
    "4H": "H4",
    "HOUR_4": "H4",
    "240M": "H4",
}


def supported_timeframes_message() -> str:
    return ", ".join(CANONICAL_TIMEFRAMES)


def normalize_timeframe(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("timeframe is required")
    normalized = _TIMEFRAME_ALIASES.get(raw.upper())
    if normalized is None:
        raise ValueError(f"timeframe must be one of: {supported_timeframes_message()}")
    return normalized


def parse_limit(value: int) -> int:
    if value < 1 or value > MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    return value


def parse_datetime_param(name: str, value: str, *, end_of_day_if_date: bool) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{name} must not be empty")

    date_candidate = _try_parse_date(raw)
    if date_candidate is not None:
        clock = time(23, 59, 59, 999999, tzinfo=timezone.utc) if end_of_day_if_date else time(0, 0, 0, tzinfo=timezone.utc)
        return datetime.combine(date_candidate, clock)

    iso_candidate = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_candidate)
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO datetime or YYYY-MM-DD") from exc

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def serialize_history_payload(
    *,
    instrument: str,
    timeframe: str,
    candles: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "instrument": instrument,
        "timeframe": timeframe,
        "count": len(candles),
        "candles": candles,
    }


def _try_parse_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None
