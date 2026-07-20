"""
Economic calendar for major US macro events (P1-5).

Provides the dates/times of the market-moving releases a systematic trader wants
to sidestep: FOMC rate decisions, CPI, Non-Farm Payrolls (NFP), and GDP.  Used
by :mod:`signals.macro_filter` to blackout new entries around those events and
by the dashboard to show what's coming up.

Sources, in priority order:

1. A **curated** list of FOMC decision dates (these are scheduled irregularly
   and cannot be derived from a rule).
2. **Rule-generated** recurring releases: NFP (first Friday, 08:30 ET), CPI
   (a mid-month heuristic, 08:30 ET) and advance GDP (quarterly, 08:30 ET).
3. An optional operator **override** file ``DATA_DIR/economic_calendar.json``
   (a list of ``{"date": "YYYY-MM-DD", "time": "HH:MM", "type": ..., "name": ...}``)
   so exact dates can be pinned or new years added without a code change.

All times are US/Eastern.  The heuristics for CPI/GDP are approximate by design
— pin exact dates via the override file when precision matters — but the FOMC
list and NFP rule are exact.  Everything is best-effort: a bad override file or
lookup error yields an empty calendar rather than raising.
"""

from __future__ import annotations

import calendar as _cal
import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from config.settings import EASTERN

log = structlog.get_logger(__name__)

EVENT_TYPES = ("fomc", "cpi", "nfp", "gdp")

# Curated FOMC decision dates (announcement day, 14:00 ET).  2025 is the Fed's
# published schedule; 2026 follows the Fed's tentative schedule and can be
# corrected via the override file once finalised.
_FOMC_DATES: List[str] = [
    # 2025 (announced)
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    # 2026 (tentative)
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]

_FOMC_TIME = time(14, 0)      # 2:00 PM ET decision
_RELEASE_TIME = time(8, 30)   # 8:30 AM ET for CPI / NFP / GDP


@dataclass(frozen=True)
class MacroEvent:
    """A single scheduled macro event, anchored at its ET release datetime."""

    when: datetime          # tz-aware US/Eastern
    event_type: str         # one of EVENT_TYPES
    name: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "datetime": self.when.isoformat(),
            "date": self.when.date().isoformat(),
            "time": self.when.strftime("%H:%M"),
            "type": self.event_type,
            "name": self.name,
        }


def _at(d: datetime | Any, t: time) -> datetime:
    """Combine a date with an ET time-of-day into a tz-aware datetime."""
    return datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=EASTERN)


def _first_friday(year: int, month: int) -> datetime:
    """First Friday of the month (NFP release day)."""
    for day in range(1, 8):
        d = datetime(year, month, day, tzinfo=EASTERN)
        if d.weekday() == _cal.FRIDAY:
            return _at(d, _RELEASE_TIME)
    return _at(datetime(year, month, 1, tzinfo=EASTERN), _RELEASE_TIME)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime:
    """The *n*-th given weekday of a month (1-based)."""
    count = 0
    for day in range(1, _cal.monthrange(year, month)[1] + 1):
        d = datetime(year, month, day, tzinfo=EASTERN)
        if d.weekday() == weekday:
            count += 1
            if count == n:
                return _at(d, _RELEASE_TIME)
    return _at(datetime(year, month, 1, tzinfo=EASTERN), _RELEASE_TIME)


def _generate_events(start: datetime, end: datetime) -> List[MacroEvent]:
    """Build the rule-based + curated events between *start* and *end* (ET)."""
    events: List[MacroEvent] = []

    # FOMC — curated.
    for iso in _FOMC_DATES:
        try:
            d = datetime.strptime(iso, "%Y-%m-%d")
        except ValueError:
            continue
        events.append(MacroEvent(_at(d, _FOMC_TIME), "fomc", "FOMC rate decision"))

    # Monthly + quarterly rule-based releases across the spanned months.
    year, month = start.year, start.month
    cursor = datetime(year, month, 1, tzinfo=EASTERN)
    guard = 0
    while cursor <= end and guard < 240:
        y, m = cursor.year, cursor.month
        # NFP — first Friday, 08:30 ET.
        events.append(MacroEvent(_first_friday(y, m), "nfp", "Non-Farm Payrolls"))
        # CPI — heuristic: second Wednesday, 08:30 ET (approximate).
        events.append(
            MacroEvent(_nth_weekday(y, m, _cal.WEDNESDAY, 2), "cpi",
                       "CPI inflation report (approx.)")
        )
        # Advance GDP — quarterly, last Thursday of Jan/Apr/Jul/Oct (approx.).
        if m in (1, 4, 7, 10):
            last_thu = _last_weekday(y, m, _cal.THURSDAY)
            events.append(MacroEvent(last_thu, "gdp", "Advance GDP (approx.)"))
        # advance a month
        month = m + 1
        year = y
        if month > 12:
            month = 1
            year += 1
        cursor = datetime(year, month, 1, tzinfo=EASTERN)
        guard += 1

    return [e for e in events if start <= e.when <= end]


def _last_weekday(year: int, month: int, weekday: int) -> datetime:
    last_day = _cal.monthrange(year, month)[1]
    for day in range(last_day, 0, -1):
        d = datetime(year, month, day, tzinfo=EASTERN)
        if d.weekday() == weekday:
            return _at(d, _RELEASE_TIME)
    return _at(datetime(year, month, last_day, tzinfo=EASTERN), _RELEASE_TIME)


def _load_overrides(data_dir: Optional[str | Path]) -> List[MacroEvent]:
    """Load operator-supplied events from ``DATA_DIR/economic_calendar.json``."""
    if not data_dir:
        return []
    path = Path(data_dir) / "economic_calendar.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("economic_calendar.override_load_failed", error=str(exc))
        return []
    items = raw.get("events") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out: List[MacroEvent] = []
    for it in items:
        try:
            d = datetime.strptime(str(it["date"]), "%Y-%m-%d")
            t = time.fromisoformat(str(it.get("time", "08:30")))
            etype = str(it.get("type", "")).lower()
            if etype not in EVENT_TYPES:
                continue
            out.append(MacroEvent(_at(d, t), etype, str(it.get("name", etype.upper()))))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def get_events(
    now: Optional[datetime] = None,
    lookahead_days: int = 45,
    lookback_days: int = 2,
    data_dir: Optional[str | Path] = None,
    event_types: Optional[List[str]] = None,
) -> List[MacroEvent]:
    """Return macro events in ``[now - lookback, now + lookahead]``, sorted.

    Merges the curated/rule-based calendar with any override-file events
    (overrides win on an exact ``(date, type)`` match).  Never raises.
    """
    now = now or datetime.now(tz=EASTERN)
    if now.tzinfo is None:
        now = now.replace(tzinfo=EASTERN)
    start = now - timedelta(days=lookback_days)
    end = now + timedelta(days=lookahead_days)
    try:
        generated = _generate_events(start, end)
    except Exception as exc:  # noqa: BLE001
        log.warning("economic_calendar.generate_failed", error=str(exc))
        generated = []
    overrides = _load_overrides(data_dir)

    # Overrides replace generated events sharing the same date + type.
    override_keys = {(e.when.date(), e.event_type) for e in overrides}
    merged = [
        e for e in generated if (e.when.date(), e.event_type) not in override_keys
    ]
    merged.extend(e for e in overrides if start <= e.when <= end)

    if event_types:
        wanted = {t.lower() for t in event_types}
        merged = [e for e in merged if e.event_type in wanted]
    merged.sort(key=lambda e: e.when)
    return merged


def upcoming_events(
    now: Optional[datetime] = None,
    days: int = 14,
    data_dir: Optional[str | Path] = None,
) -> List[Dict[str, Any]]:
    """Return upcoming events (from *now* forward) as dicts for the dashboard."""
    now = now or datetime.now(tz=EASTERN)
    events = get_events(now, lookahead_days=days, lookback_days=0, data_dir=data_dir)
    return [
        {**e.to_dict(),
         "days_until": (e.when.date() - now.date()).days}
        for e in events
    ]


def active_blackout(
    now: Optional[datetime] = None,
    hours_before: float = 24.0,
    hours_after: float = 12.0,
    data_dir: Optional[str | Path] = None,
    event_types: Optional[List[str]] = None,
) -> Optional[MacroEvent]:
    """Return the macro event whose blackout window contains *now*, else None.

    The window for an event at time ``T`` is ``[T - hours_before, T + hours_after]``.
    When several overlap, the soonest-starting active one is returned.
    """
    now = now or datetime.now(tz=EASTERN)
    if now.tzinfo is None:
        now = now.replace(tzinfo=EASTERN)
    events = get_events(
        now,
        lookahead_days=max(2, int(hours_before / 24) + 2),
        lookback_days=max(1, int(hours_after / 24) + 2),
        data_dir=data_dir,
        event_types=event_types,
    )
    for e in events:
        window_start = e.when - timedelta(hours=hours_before)
        window_end = e.when + timedelta(hours=hours_after)
        if window_start <= now <= window_end:
            return e
    return None
