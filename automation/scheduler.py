"""
A tiny dependency-free scheduler (features 10 & 11).

Runs daily / weekly jobs from a background thread — enough to fire the P&L
email reports and nightly backtests without pulling in APScheduler or cron.

The scheduling *decision* is factored into the pure :func:`is_due` /
:meth:`SimpleScheduler.tick` so it unit-tests deterministically with an injected
clock; the background thread is a thin wrapper that calls ``tick(datetime.now())``
every ``poll_seconds``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_WEEKDAYS = {
    "MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6,
}


def parse_hhmm(value: str) -> tuple[int, int]:
    """Parse ``"HH:MM"`` into ``(hour, minute)``; raises ValueError on bad input."""
    hh, mm = str(value).strip().split(":")
    hour, minute = int(hh), int(mm)
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"Invalid time: {value!r}")
    return hour, minute


def parse_weekday(value: str) -> int:
    """Parse a weekday name (``"FRI"``) into ``0..6`` (Mon=0)."""
    key = str(value).strip().upper()[:3]
    if key not in _WEEKDAYS:
        raise ValueError(f"Invalid weekday: {value!r}")
    return _WEEKDAYS[key]


@dataclass
class Job:
    """A scheduled job: fires once per day (or per week) at ``(hour, minute)``."""

    name: str
    hour: int
    minute: int
    callback: Callable[[], None]
    kind: str = "daily"  # "daily" | "weekly"
    weekday: int = 0  # only for weekly jobs (Mon=0)
    last_run: Optional[datetime] = field(default=None)


def is_due(job: Job, now: datetime, last_run: Optional[datetime]) -> bool:
    """Return whether *job* should fire at *now* given its *last_run*.

    A job is due when the clock has reached its scheduled ``HH:MM`` for the
    current day (and, for weekly jobs, the current weekday) and it has not
    already run today.
    """
    if job.kind == "weekly" and now.weekday() != job.weekday:
        return False
    scheduled = now.replace(
        hour=job.hour, minute=job.minute, second=0, microsecond=0
    )
    if now < scheduled:
        return False
    if last_run is not None and last_run.date() >= now.date():
        return False
    return True


class SimpleScheduler:
    """A minimal in-process daily/weekly job runner."""

    def __init__(self, poll_seconds: float = 30.0) -> None:
        self._jobs: List[Job] = []
        self._poll = poll_seconds
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._log = log.bind(component="SimpleScheduler")

    # -------------------------------------------------------------- registration

    def add_daily(self, name: str, hhmm: str, callback: Callable[[], None]) -> None:
        hour, minute = parse_hhmm(hhmm)
        with self._lock:
            self._jobs.append(Job(name, hour, minute, callback, kind="daily"))

    def add_weekly(
        self, name: str, weekday: str, hhmm: str, callback: Callable[[], None]
    ) -> None:
        hour, minute = parse_hhmm(hhmm)
        with self._lock:
            self._jobs.append(
                Job(name, hour, minute, callback, kind="weekly",
                    weekday=parse_weekday(weekday))
            )

    @property
    def jobs(self) -> List[Job]:
        with self._lock:
            return list(self._jobs)

    # -------------------------------------------------------------------- tick

    def tick(self, now: datetime) -> List[str]:
        """Run any due jobs and return the names that fired.  Never raises."""
        fired: List[str] = []
        for job in self.jobs:
            if is_due(job, now, job.last_run):
                job.last_run = now
                try:
                    job.callback()
                    fired.append(job.name)
                    self._log.info("scheduler.job_fired", job=job.name)
                except Exception as exc:  # noqa: BLE001 -- one bad job must not kill the loop
                    self._log.warning("scheduler.job_failed", job=job.name, error=str(exc))
        return fired

    # ------------------------------------------------------------- thread mgmt

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="SimpleScheduler", daemon=True
            )
            self._thread.start()
            self._log.info("scheduler.started", jobs=[j.name for j in self._jobs])

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick(datetime.now())
            except Exception as exc:  # noqa: BLE001
                self._log.warning("scheduler.tick_failed", error=str(exc))
            self._stop.wait(self._poll)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._log.info("scheduler.stopped")


def build_scheduler(settings) -> Optional[SimpleScheduler]:
    """Assemble the configured scheduler from *settings*, or ``None`` if off.

    Wires the daily/weekly P&L report and the nightly backtest jobs according
    to the ``PNL_REPORT_*`` / ``SCHEDULED_BACKTEST_*`` settings.
    """
    if not getattr(settings, "SCHEDULER_ENABLED", False):
        return None
    scheduler = SimpleScheduler()
    added = False

    if getattr(settings, "PNL_REPORT_ENABLED", False):
        from automation.pnl_report import send_report

        scheduler.add_daily(
            "pnl_daily", settings.PNL_REPORT_DAILY_TIME,
            lambda: send_report(settings, "daily"),
        )
        added = True
        if getattr(settings, "PNL_REPORT_WEEKLY_ENABLED", True):
            scheduler.add_weekly(
                "pnl_weekly", settings.PNL_REPORT_WEEKLY_DAY,
                settings.PNL_REPORT_DAILY_TIME,
                lambda: send_report(settings, "weekly"),
            )

    if getattr(settings, "SCHEDULED_BACKTEST_ENABLED", False):
        from automation.scheduled_backtest import run_nightly_backtests

        scheduler.add_daily(
            "nightly_backtest", settings.SCHEDULED_BACKTEST_TIME,
            lambda: run_nightly_backtests(settings),
        )
        added = True

    return scheduler if added else None
