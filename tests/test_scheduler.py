"""Tests for the simple scheduler (features 10 & 11)."""

from __future__ import annotations

from datetime import datetime

import pytest

from automation.scheduler import (
    Job,
    SimpleScheduler,
    is_due,
    parse_hhmm,
    parse_weekday,
)


def test_parse_helpers():
    assert parse_hhmm("17:05") == (17, 5)
    assert parse_weekday("fri") == 4
    with pytest.raises(ValueError):
        parse_hhmm("25:00")
    with pytest.raises(ValueError):
        parse_weekday("xyz")


def _job(**kw):
    kw.setdefault("callback", lambda: None)
    kw.setdefault("hour", 17)
    kw.setdefault("minute", 0)
    kw.setdefault("name", "t")
    return Job(**kw)


def test_daily_due_after_time_once_per_day():
    job = _job()
    before = datetime(2026, 7, 1, 16, 59)
    at = datetime(2026, 7, 1, 17, 0)
    assert not is_due(job, before, None)
    assert is_due(job, at, None)
    # Already ran today -> not due again.
    assert not is_due(job, datetime(2026, 7, 1, 18, 0), at)
    # Next day -> due again.
    assert is_due(job, datetime(2026, 7, 2, 17, 1), at)


def test_weekly_only_on_weekday():
    job = _job(kind="weekly", weekday=4)  # Friday
    thursday = datetime(2026, 7, 2, 17, 0)  # 2026-07-02 is Thursday
    friday = datetime(2026, 7, 3, 17, 0)
    assert not is_due(job, thursday, None)
    assert is_due(job, friday, None)


def test_tick_runs_due_jobs_once():
    calls = {"n": 0}
    sched = SimpleScheduler()
    sched.add_daily("job", "09:00", lambda: calls.__setitem__("n", calls["n"] + 1))
    fired = sched.tick(datetime(2026, 7, 1, 9, 0))
    assert fired == ["job"] and calls["n"] == 1
    # Same day, later -> does not re-fire.
    assert sched.tick(datetime(2026, 7, 1, 10, 0)) == []
    assert calls["n"] == 1


def test_tick_isolates_failing_job():
    def boom():
        raise RuntimeError("bad job")

    sched = SimpleScheduler()
    sched.add_daily("bad", "09:00", boom)
    sched.add_daily("good", "09:00", lambda: None)
    fired = sched.tick(datetime(2026, 7, 1, 9, 0))
    assert fired == ["good"]  # bad job swallowed, good job still ran


def test_build_scheduler_disabled(settings):
    from automation.scheduler import build_scheduler

    assert build_scheduler(settings) is None
