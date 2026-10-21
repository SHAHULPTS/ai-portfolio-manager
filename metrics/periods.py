"""Turn "YTD", "last 3 years", "since inception" into exact dates.

The planner LLM picks a preset or gives explicit dates; this module is the only place that
turns those into a (start, end) pair, so every metric in an answer covers the same window.
A period's returns are those of trading days with start <= day <= end, each measured from the
previous trading day's close. So "2025" really is 2025: its first return is 1 January's,
measured from the 31 December 2024 close.
"""

import pandas as pd

PRESETS = ("since_inception", "ytd", "mtd", "1m", "3m", "6m", "1y", "3y", "5y")


def resolve(
    preset: str | None,
    as_of: pd.Timestamp,
    inception: pd.Timestamp,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return (start, end), clipped to [inception, as_of]. Explicit dates beat a preset."""
    end = min(pd.Timestamp(end), as_of) if end is not None else as_of
    if start is not None:
        start = pd.Timestamp(start)
    else:
        preset = preset or "since_inception"
        if preset not in PRESETS:
            raise ValueError(f"unknown period {preset!r}; expected one of {PRESETS}")
        offsets = {
            "since_inception": None,
            "ytd": pd.Timestamp(year=end.year, month=1, day=1),
            "mtd": pd.Timestamp(year=end.year, month=end.month, day=1),
            "1m": end - pd.DateOffset(months=1) + pd.Timedelta(days=1),
            "3m": end - pd.DateOffset(months=3) + pd.Timedelta(days=1),
            "6m": end - pd.DateOffset(months=6) + pd.Timedelta(days=1),
            "1y": end - pd.DateOffset(years=1) + pd.Timedelta(days=1),
            "3y": end - pd.DateOffset(years=3) + pd.Timedelta(days=1),
            "5y": end - pd.DateOffset(years=5) + pd.Timedelta(days=1),
        }
        start = offsets[preset] or inception
    start = max(start, inception)
    if start > end:
        raise ValueError(f"period starts {start.date()} after it ends {end.date()}")
    return start, end
