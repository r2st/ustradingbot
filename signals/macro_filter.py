"""
Macro-event entry filter (P1-5).

The mirror of :mod:`signals.earnings_filter` for market-wide events: before a
new position is opened the engine asks this filter whether *now* falls inside a
blackout window around a scheduled FOMC / CPI / NFP / GDP release and, if so,
whether to **block** the entry or merely **flag** it.

Modes (``settings.MACRO_FILTER_MODE``):

* ``off``   — gate disabled.
* ``flag``  — never rejects; annotates ``sig.raw_data["macro_flag"]`` and returns
              ``mode="flag"``.
* ``block`` — rejects entries inside the blackout window.

The window is ``[event - MACRO_BLACKOUT_HOURS_BEFORE, event +
MACRO_BLACKOUT_HOURS_AFTER]``; which event types are active is set by
``settings.MACRO_BLACKOUT_EVENT_TYPES``.  Everything is **fail-open** — any
calendar error means "allowed".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, List, Optional, Tuple

import structlog

from config.settings import EASTERN

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass
class MacroCheck:
    """Outcome of a macro-proximity check for one signal."""

    allowed: bool
    mode: str          # "block" | "flag" | "ok"
    reason: str
    event_type: Optional[str] = None
    event_time: Optional[str] = None

    def as_tuple(self) -> Tuple[bool, str, str]:
        return self.allowed, self.mode, self.reason


class MacroEntryFilter:
    """Gate new entries on proximity to a scheduled macro event.

    Args:
        settings: Application settings (reads the ``MACRO_*`` fields).
        blackout_fetcher: ``fetcher(now, before, after, data_dir, types) ->
            Optional[MacroEvent]``; defaults to
            :func:`data.economic_calendar.active_blackout`.  Injectable for tests.
        now_fn: Injectable clock returning a tz-aware ET datetime.
    """

    def __init__(
        self,
        settings: Any,
        blackout_fetcher: Optional[Callable[..., Any]] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._settings = settings
        self._fetcher = blackout_fetcher
        self._now = now_fn or (lambda: datetime.now(tz=EASTERN))
        self._log = log.bind(component="MacroEntryFilter")

    def check(self, signal: Any) -> MacroCheck:
        """Return the macro verdict for *signal*.  Never raises."""
        mode = str(
            getattr(self._settings, "MACRO_FILTER_MODE", "off") or "off"
        ).lower()
        if mode not in ("flag", "block"):
            return MacroCheck(True, "ok", "macro filter disabled")

        before = float(getattr(self._settings, "MACRO_BLACKOUT_HOURS_BEFORE", 24.0))
        after = float(getattr(self._settings, "MACRO_BLACKOUT_HOURS_AFTER", 12.0))
        types = self._event_types()

        try:
            event = self._resolve(before, after, types)
        except Exception as exc:  # noqa: BLE001 -- fail open on any lookup error
            self._log.warning("macro_filter.lookup_failed", error=str(exc))
            return MacroCheck(True, "ok", "macro lookup failed (fail-open)")

        if event is None:
            return MacroCheck(True, "ok", "no active macro blackout")

        reason = (
            f"{event.name} at {event.when.strftime('%Y-%m-%d %H:%M ET')} "
            f"(blackout -{before:g}h/+{after:g}h)"
        )
        self._annotate(signal, event)
        if mode == "flag":
            return MacroCheck(True, "flag", reason, event.event_type,
                              event.when.isoformat())
        return MacroCheck(False, "block", reason, event.event_type,
                          event.when.isoformat())

    # ---------------------------------------------------------------- internal

    def _event_types(self) -> List[str]:
        raw = getattr(self._settings, "MACRO_BLACKOUT_EVENT_TYPES", None)
        if isinstance(raw, str):
            return [t.strip().lower() for t in raw.split(",") if t.strip()]
        if isinstance(raw, (list, tuple)):
            return [str(t).strip().lower() for t in raw if str(t).strip()]
        return ["fomc", "cpi", "nfp"]

    def _resolve(self, before: float, after: float, types: List[str]) -> Any:
        data_dir = getattr(self._settings, "DATA_DIR", None)
        if self._fetcher is not None:
            return self._fetcher(self._now(), before, after, data_dir, types)
        from data.economic_calendar import active_blackout

        return active_blackout(
            now=self._now(),
            hours_before=before,
            hours_after=after,
            data_dir=data_dir,
            event_types=types,
        )

    @staticmethod
    def _annotate(signal: Any, event: Any) -> None:
        raw = getattr(signal, "raw_data", None)
        if isinstance(raw, dict):
            raw["macro_flag"] = {
                "event_type": event.event_type,
                "event_time": event.when.isoformat(),
                "name": event.name,
            }
