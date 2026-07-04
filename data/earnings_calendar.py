"""
Earnings calendar (feature 5).

Resolves the next scheduled earnings date for each watchlist symbol (via
yfinance) and packages the results for the dashboard.  Per-symbol lookups are
cached for a few hours because earnings dates rarely change intraday.

:func:`upcoming_earnings` is pure given an injectable ``fetcher`` so it tests
without touching the network.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from threading import RLock
from typing import Callable, Dict, List, Optional, Sequence

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_CACHE_TTL_SECONDS = 6 * 3600.0
_cache: Dict[str, tuple[float, Optional[date]]] = {}
_cache_lock = RLock()


@dataclass
class EarningsEntry:
    """Next-earnings info for a single symbol."""

    symbol: str
    earnings_date: Optional[date]
    days_until: Optional[int]
    is_upcoming: bool

    def to_dict(self) -> Dict[str, object]:
        return {
            "symbol": self.symbol,
            "earnings_date": self.earnings_date.isoformat() if self.earnings_date else None,
            "days_until": self.days_until,
            "is_upcoming": self.is_upcoming,
        }


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def next_earnings_date(symbol: str) -> Optional[date]:
    """Return the next future earnings date for *symbol* (cached; yfinance)."""
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(symbol)
        if entry is not None and now - entry[0] <= _CACHE_TTL_SECONDS:
            return entry[1]
    result = _yf_next_earnings_date(symbol)
    with _cache_lock:
        _cache[symbol] = (now, result)
    return result


def _yf_next_earnings_date(symbol: str) -> Optional[date]:
    """Best-effort next earnings date from yfinance.  Returns None on any error."""
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        today = date.today()
        candidates: List[date] = []

        # Preferred: get_earnings_dates() returns a DataFrame indexed by datetime.
        try:
            df = ticker.get_earnings_dates(limit=12)
            if df is not None and len(df):
                for idx in df.index:
                    d = getattr(idx, "date", lambda: None)()
                    if d and d >= today:
                        candidates.append(d)
        except Exception:  # noqa: BLE001
            pass

        # Fallback: the .calendar dict/DataFrame with an "Earnings Date".
        if not candidates:
            try:
                cal = ticker.calendar
                value = None
                if isinstance(cal, dict):
                    value = cal.get("Earnings Date")
                elif cal is not None and "Earnings Date" in getattr(cal, "index", []):
                    value = cal.loc["Earnings Date"].iloc[0]
                for item in (value if isinstance(value, (list, tuple)) else [value]):
                    if item is None:
                        continue
                    d = item if isinstance(item, date) else getattr(item, "date", lambda: None)()
                    if d and d >= today:
                        candidates.append(d)
            except Exception:  # noqa: BLE001
                pass

        return min(candidates) if candidates else None
    except Exception as exc:  # noqa: BLE001
        log.warning("earnings.lookup_failed", symbol=symbol, error=str(exc))
        return None


def upcoming_earnings(
    symbols: Sequence[str],
    fetcher: Optional[Callable[[str], Optional[date]]] = None,
    today: Optional[date] = None,
    horizon_days: int = 90,
) -> List[EarningsEntry]:
    """Return earnings entries for *symbols*, upcoming ones first.

    Args:
        symbols: Tickers to resolve.
        fetcher: ``fetcher(symbol) -> Optional[date]``; defaults to
            :func:`next_earnings_date`.
        today: Reference date (defaults to ``date.today()``).
        horizon_days: An entry ``is_upcoming`` when its date is within
            ``[today, today + horizon_days]``.
    """
    fetcher = fetcher or next_earnings_date
    today = today or date.today()
    entries: List[EarningsEntry] = []
    for symbol in symbols:
        try:
            edate = fetcher(symbol)
        except Exception as exc:  # noqa: BLE001 -- one bad symbol must not break the page
            log.warning("earnings.fetcher_error", symbol=symbol, error=str(exc))
            edate = None
        if edate is not None:
            days_until = (edate - today).days
            is_upcoming = 0 <= days_until <= horizon_days
        else:
            days_until = None
            is_upcoming = False
        entries.append(EarningsEntry(str(symbol).upper(), edate, days_until, is_upcoming))

    # Sort: known upcoming dates soonest-first, then other known dates, then
    # unknown dates last (alphabetically).
    def _key(e: EarningsEntry):
        if e.earnings_date is None:
            return (2, 0, e.symbol)
        return (0 if e.is_upcoming else 1, e.days_until if e.days_until is not None else 10**9, e.symbol)

    entries.sort(key=_key)
    return entries
