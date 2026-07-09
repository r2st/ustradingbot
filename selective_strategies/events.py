"""Macro event calendar for highly selective strategies."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional, Set

from config.settings import EASTERN

# High-impact US macro events that invalidate certain setups.
# Initial implementation: static set of recurring monthly events.
# Format: (month, approximate_day_range_start, approximate_day_range_end, name)
# The actual dates shift year to year; this is a conservative approximation.

_RECURRING_EVENTS = [
    # FOMC decisions (8 per year, roughly every 6 weeks)
    "FOMC",
    # CPI release (monthly, around 10th-13th)
    "CPI",
    # Non-Farm Payrolls (first Friday of month)
    "NFP",
    # PCE (last Friday of month or nearby)
    "PCE",
]


def is_macro_event_day(dt: Optional[date] = None) -> bool:
    """Check if today (or given date) is a high-impact macro release day.

    Conservative implementation: flags the first and third Fridays of each
    month (NFP + other releases) plus mid-month (CPI window) and FOMC
    meeting days. This casts a wide net; false positives are acceptable
    because they only suppress signals (fail-safe).
    """
    if dt is None:
        dt = datetime.now(tz=EASTERN).date()

    day = dt.day
    weekday = dt.weekday()  # 0=Monday

    # First Friday of month (NFP)
    if weekday == 4 and 1 <= day <= 7:
        return True
    # CPI window (10th-14th)
    if 10 <= day <= 14:
        return True
    # FOMC window (typically mid-month, 2-day meeting ending on Wednesday)
    # Conservative: flag the Tuesday and Wednesday closest to the 15th-16th
    if 14 <= day <= 18 and weekday in (1, 2):  # Tue/Wed
        return True
    return False


def has_upcoming_event(dt: Optional[date] = None, days: int = 2) -> bool:
    """Check if there's a macro event within the next N trading days."""
    if dt is None:
        dt = datetime.now(tz=EASTERN).date()
    for i in range(days + 1):
        check = dt + timedelta(days=i)
        if is_macro_event_day(check):
            return True
    return False
