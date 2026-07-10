"""
Overnight-gap entry filter (Feature 4).

Uses the *true* extended-hours quote (:mod:`data.extended_hours`) to decide
whether a morning entry should still be taken after the stock moved overnight.
A long that gapped sharply *down* into the open has lost its setup (the
protective stop is now far away and the edge is gone); a long that already
gapped far *up* is a chase.  Shorts mirror the logic.

The verdict is one of:

* ``ok``     — no significant gap; take the trade unchanged.
* ``resize`` — a moderate adverse/extended gap; shrink the position by
               ``GAP_RESIZE_MODIFIER`` rather than skipping outright.
* ``skip``   — a large adverse gap (or an over-extended chase); reject.

**Fail-open:** disabled by default; when enabled but no extended-hours quote is
available (provider lacks support, off-hours, or any error), the entry proceeds
untouched (``ok``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass
class GapCheck:
    """Outcome of an overnight-gap check for one signal."""

    allowed: bool
    action: str            # "skip" | "resize" | "ok"
    reason: str
    size_modifier: float = 1.0
    gap_pct: Optional[float] = None


class GapEntryFilter:
    """Skip or resize entries after a significant overnight gap.

    Args:
        settings: Application settings (reads ``GAP_FILTER_ENABLED``,
            ``GAP_DOWN_SKIP_PCT``, ``GAP_UP_CHASE_PCT``, ``GAP_RESIZE_PCT``,
            ``GAP_RESIZE_MODIFIER``).
        quote_fetcher: ``fetcher(symbol) -> ExtQuote | None``; defaults to
            :func:`data.extended_hours.overnight_gap`.
    """

    def __init__(
        self,
        settings: Any,
        quote_fetcher: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self._settings = settings
        self._fetcher = quote_fetcher
        self._log = log.bind(component="GapEntryFilter")

    def check(self, signal: Any) -> GapCheck:
        """Return the gap verdict for *signal*.  Never raises."""
        s = self._settings
        if not getattr(s, "GAP_FILTER_ENABLED", False):
            return GapCheck(True, "ok", "gap filter disabled")

        symbol = str(getattr(signal, "symbol", "") or "").upper()
        direction = str(getattr(signal, "direction", "long") or "long").lower()

        try:
            quote = self._resolve_quote(symbol)
        except Exception as exc:  # noqa: BLE001 -- fail-open
            self._log.warning("gap_filter.lookup_failed", symbol=symbol, error=str(exc))
            return GapCheck(True, "ok", "extended-hours lookup failed (fail-open)")

        gap_pct = getattr(quote, "gap_pct", None) if quote is not None else None
        if gap_pct is None:
            return GapCheck(True, "ok", "no extended-hours gap data (fail-open)")

        skip_pct = float(getattr(s, "GAP_DOWN_SKIP_PCT", -0.05))
        chase_pct = float(getattr(s, "GAP_UP_CHASE_PCT", 0.08))
        resize_pct = float(getattr(s, "GAP_RESIZE_PCT", 0.03))
        resize_mod = float(getattr(s, "GAP_RESIZE_MODIFIER", 0.5))

        is_short = direction == "short"
        # For a long: adverse gap is a gap DOWN (gap_pct <= skip_pct, skip_pct<0).
        # For a short: adverse gap is a gap UP (gap_pct >= -skip_pct).
        if is_short:
            adverse_skip = gap_pct >= -skip_pct       # e.g. +5% up on a short
            adverse_resize = gap_pct >= resize_pct
            chase_skip = gap_pct <= -chase_pct        # already ran the short's way
        else:
            adverse_skip = gap_pct <= skip_pct        # e.g. -5% down on a long
            adverse_resize = gap_pct <= -resize_pct
            chase_skip = gap_pct >= chase_pct          # already gapped up big

        pct_str = f"{gap_pct * 100:+.1f}%"
        if adverse_skip:
            return GapCheck(
                False, "skip", f"adverse overnight gap {pct_str}", gap_pct=gap_pct
            )
        if chase_skip:
            return GapCheck(
                False, "skip", f"over-extended overnight gap {pct_str} (chase)",
                gap_pct=gap_pct,
            )
        if adverse_resize:
            return GapCheck(
                True, "resize", f"moderate adverse gap {pct_str}; resizing",
                size_modifier=resize_mod, gap_pct=gap_pct,
            )
        return GapCheck(True, "ok", f"overnight gap {pct_str} within tolerance",
                        gap_pct=gap_pct)

    def _resolve_quote(self, symbol: str) -> Any:
        if self._fetcher is not None:
            return self._fetcher(symbol)
        from data.extended_hours import overnight_gap

        return overnight_gap(symbol, self._settings)
