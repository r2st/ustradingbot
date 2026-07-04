"""Tests for the earnings calendar (feature 5)."""

from __future__ import annotations

from datetime import date

from data.earnings_calendar import EarningsEntry, upcoming_earnings


def test_days_until_and_upcoming_flag():
    today = date(2026, 7, 1)
    dates = {
        "AAPL": date(2026, 7, 1),   # today, day 0
        "MSFT": date(2026, 9, 29),  # exactly horizon (90 days)
        "NVDA": date(2026, 9, 30),  # horizon + 1 -> not upcoming
    }
    entries = upcoming_earnings(dates.keys(), fetcher=lambda s: dates[s],
                                today=today, horizon_days=90)
    by = {e.symbol: e for e in entries}
    assert by["AAPL"].days_until == 0 and by["AAPL"].is_upcoming
    assert by["MSFT"].days_until == 90 and by["MSFT"].is_upcoming
    assert by["NVDA"].days_until == 91 and not by["NVDA"].is_upcoming


def test_sorting_soonest_first():
    today = date(2026, 7, 1)
    dates = {"A": date(2026, 8, 1), "B": date(2026, 7, 10), "C": None}
    entries = upcoming_earnings(["A", "B", "C"], fetcher=lambda s: dates[s], today=today)
    assert [e.symbol for e in entries] == ["B", "A", "C"]


def test_none_date_handled():
    entries = upcoming_earnings(["X"], fetcher=lambda s: None, today=date(2026, 7, 1))
    e = entries[0]
    assert e.earnings_date is None and e.days_until is None and not e.is_upcoming


def test_fetcher_error_does_not_break_others():
    today = date(2026, 7, 1)

    def fetcher(sym):
        if sym == "BAD":
            raise RuntimeError("boom")
        return date(2026, 7, 15)

    entries = upcoming_earnings(["BAD", "GOOD"], fetcher=fetcher, today=today)
    by = {e.symbol: e for e in entries}
    assert by["BAD"].earnings_date is None
    assert by["GOOD"].is_upcoming


def test_to_dict():
    e = EarningsEntry("AAPL", date(2026, 7, 5), 4, True)
    d = e.to_dict()
    assert d == {"symbol": "AAPL", "earnings_date": "2026-07-05",
                 "days_until": 4, "is_upcoming": True}
