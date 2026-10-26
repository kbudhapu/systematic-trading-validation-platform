"""
Uptrend session regime filter — shared between vectorized sweeps and live execution.

Matches the sweep logic in scripts/run_exhaustive_sweep.py: long entries are only
permitted on calendar dates where the daily close trades above its 200-day SMA.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Sequence

import numpy as np

DEFAULT_REGIME_SMA_PERIOD = 200


def build_uptrend_session_dates(
    timestamps: Sequence[datetime],
    closes: Sequence[float],
    *,
    period: int = DEFAULT_REGIME_SMA_PERIOD,
) -> set[str]:
    """Return ISO session dates where daily close > trailing SMA(period)."""
    if period <= 0:
        return set()
    close_values = [float(value) for value in closes]
    if len(close_values) < period or len(timestamps) != len(close_values):
        return set()

    allowed: set[str] = set()
    running = sum(close_values[:period])
    for idx in range(period - 1, len(close_values)):
        if idx >= period:
            running += close_values[idx] - close_values[idx - period]
        sma = running / period
        if close_values[idx] > sma:
            session_date = timestamps[idx]
            if isinstance(session_date, datetime):
                allowed.add(session_date.date().isoformat())
            elif isinstance(session_date, date):
                allowed.add(session_date.isoformat())
            else:
                allowed.add(str(session_date)[:10])
    return allowed


def build_regime_mask(
    timestamps: Sequence[datetime],
    allowed_dates: set[str] | None,
) -> np.ndarray:
    """Per-bar mask aligned with vectorized_mr regime_mask semantics."""
    mask = np.ones(len(timestamps), dtype=np.int8)
    if allowed_dates is None:
        return mask
    mask[:] = 0
    for idx, ts in enumerate(timestamps):
        session = ts.date().isoformat() if isinstance(ts, datetime) else str(ts)[:10]
        if session in allowed_dates:
            mask[idx] = 1
    return mask


def is_long_entry_regime_allowed(
    bar_timestamp: datetime,
    allowed_dates: set[str] | None,
    *,
    regime_filter_enabled: bool,
) -> bool:
    """True when a long entry is permitted under sweep-equivalent regime rules."""
    if not regime_filter_enabled:
        return True
    if allowed_dates is None:
        return False
    return bar_timestamp.date().isoformat() in allowed_dates


def daily_closes_from_intraday_bars(
    timestamps: Iterable[datetime],
    closes: Iterable[float],
) -> tuple[list[datetime], list[float]]:
    """Collapse intraday bars to one close per calendar day (last bar of each day)."""
    by_day: dict[str, tuple[datetime, float]] = {}
    for ts, close in zip(timestamps, closes, strict=False):
        if not isinstance(ts, datetime):
            continue
        day_key = ts.date().isoformat()
        by_day[day_key] = (ts, float(close))
    ordered_days = sorted(by_day.keys())
    daily_ts = [by_day[day][0] for day in ordered_days]
    daily_closes = [by_day[day][1] for day in ordered_days]
    return daily_ts, daily_closes
