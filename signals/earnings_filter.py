"""
Earnings entry filter (Feature 1a).

Promotes the coarse 14-day AI-layer earnings blackout into a first-class,
configurable pre-entry gate.  Before a new position is opened the engine asks
this filter whether the symbol has a scheduled earnings report inside the
configured window and, if so, whether to *block* the entry (binary gap risk) or
merely *flag* it (annotate the signal and let it through).

Modes (``settings.EARNINGS_FILTER_MODE``):

* ``off``   — gate disabled (the AI blackout still applies upstream).
* ``flag``  — never rejects; annotates ``sig.raw_data["earnings_flag"]`` for the
              dashboard and returns ``mode="flag"``.
* ``block`` — rejects entries whose earnings fall within
              ``settings.EARNINGS_BLOCK_DAYS`` calendar days.

PEAD is exempt — it is *designed* to trade the post-earnings drift, so it must
never be blocked by an upcoming (or just-passed) earnings date.

The lookup is :func:`data.earnings_calendar.next_earnings_date` (6 h cached,
yfinance-backed).  Everything is **fail-open**: a missing date or any error
means "allowed" so a broken calendar never halts the scan.  ETFs have no
single-name earnings date, so they naturally pass through.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Optional, Tuple

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Strategies that must never be earnings-blocked (they trade the event/drift).
_EXEMPT_STRATEGIES = {"pead", "hs_pead_drift"}


@dataclass
class EarningsCheck:
    """Outcome of an earnings-proximity check for one signal."""

    allowed: bool
    mode: str          # "block" | "flag" | "ok"
    reason: str
    days_until: Optional[int] = None
    earnings_date: Optional[date] = None

    def as_tuple(self) -> Tuple[bool, str, str]:
        return self.allowed, self.mode, self.reason


class EarningsEntryFilter:
    """Gate new entries on proximity to a scheduled earnings report.

    Args:
        settings: Application settings (reads ``EARNINGS_FILTER_MODE`` and
            ``EARNINGS_BLOCK_DAYS``).
        date_fetcher: ``fetcher(symbol) -> Optional[date]`` returning the next
            earnings date; defaults to
            :func:`data.earnings_calendar.next_earnings_date`.
        today: Injectable reference date for deterministic tests.
    """

    def __init__(
        self,
        settings: Any,
        date_fetcher: Optional[Callable[[str], Optional[date]]] = None,
        today: Optional[Callable[[], date]] = None,
    ) -> None:
        self._settings = settings
        self._fetcher = date_fetcher
        self._today = today or date.today
        self._log = log.bind(component="EarningsEntryFilter")

    # ------------------------------------------------------------------ public

    def check(self, signal: Any) -> EarningsCheck:
        """Return the earnings verdict for *signal*.  Never raises.

        In ``flag`` mode a positive hit annotates
        ``signal.raw_data["earnings_flag"]`` and still returns ``allowed=True``.
        """
        mode = str(getattr(self._settings, "EARNINGS_FILTER_MODE", "off") or "off").lower()
        if mode not in ("flag", "block"):
            return EarningsCheck(True, "ok", "earnings filter disabled")

        symbol = str(getattr(signal, "symbol", "") or "").upper()
        strategy = str(getattr(signal, "strategy", "") or "").lower()
        if strategy in _EXEMPT_STRATEGIES:
            return EarningsCheck(True, "ok", "PEAD is exempt from the earnings gate")

        block_days = int(getattr(self._settings, "EARNINGS_BLOCK_DAYS", 2))

        try:
            edate = self._resolve_date(symbol)
        except Exception as exc:  # noqa: BLE001 -- fail-open on any lookup error
            self._log.warning("earnings_filter.lookup_failed", symbol=symbol, error=str(exc))
            return EarningsCheck(True, "ok", "earnings lookup failed (fail-open)")

        if edate is None:
            return EarningsCheck(True, "ok", "no scheduled earnings date")

        days_until = (edate - self._today()).days
        # Only *upcoming* earnings inside the window matter for entry risk.
        within = 0 <= days_until <= block_days
        if not within:
            return EarningsCheck(
                True, "ok", f"earnings in {days_until}d (outside {block_days}d window)",
                days_until=days_until, earnings_date=edate,
            )

        reason = f"earnings in {days_until}d (<= {block_days}d window)"
        if mode == "flag":
            self._annotate(signal, days_until, edate)
            return EarningsCheck(True, "flag", reason, days_until=days_until, earnings_date=edate)
        # block
        self._annotate(signal, days_until, edate)
        return EarningsCheck(False, "block", reason, days_until=days_until, earnings_date=edate)

    # ---------------------------------------------------------------- internal

    def _resolve_date(self, symbol: str) -> Optional[date]:
        if self._fetcher is not None:
            return self._fetcher(symbol)
        from data.earnings_calendar import next_earnings_date

        return next_earnings_date(symbol)

    @staticmethod
    def _annotate(signal: Any, days_until: int, edate: date) -> None:
        """Record the earnings flag on the signal for the dashboard."""
        raw = getattr(signal, "raw_data", None)
        if isinstance(raw, dict):
            raw["earnings_flag"] = {
                "days_until": days_until,
                "earnings_date": edate.isoformat(),
            }
