from __future__ import annotations

import pandas as pd


def is_blocked_after_friday_cutoff(setup_time_utc: pd.Timestamp, *, cutoff_hour: int, tz: str) -> bool:
    local = setup_time_utc.tz_convert(tz)
    if local.weekday() != 4:
        return False
    cutoff_local = local.normalize() + pd.Timedelta(hours=cutoff_hour)
    cutoff_utc = cutoff_local.tz_convert("UTC")
    return setup_time_utc >= cutoff_utc
