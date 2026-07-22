"""Tests for the two log-noise fixes: ETF earnings ERROR + yfinance downgrade."""

from __future__ import annotations

import logging

import data.earnings as earnings
import data.earnings_calendar as earnings_calendar
from logging_config import _YFinanceBenignFilter


# ---------------------------------------------------------------------------
# ETF earnings short-circuit — SOXL (a leveraged ETF) has no earnings date and
# must never reach yfinance (which logs a spurious ERROR for it).
# ---------------------------------------------------------------------------


def test_next_earnings_date_skips_etf_without_yfinance(monkeypatch):
    def _boom(_symbol):  # pragma: no cover - must not be called
        raise AssertionError("yfinance was queried for an ETF")

    monkeypatch.setattr(earnings_calendar, "_yf_next_earnings_date", _boom)
    assert earnings_calendar.next_earnings_date("SOXL") is None


def test_get_earnings_date_skips_etf(monkeypatch):
    class _Boom:  # pragma: no cover - must not be constructed
        def __init__(self, *_a, **_k):
            raise AssertionError("yfinance was queried for an ETF")

    monkeypatch.setattr(earnings.yf, "Ticker", _Boom)
    assert earnings.get_earnings_date("SOXL") is None
    # is_earnings_within_days delegates to get_earnings_date -> also short-circuits.
    assert earnings.is_earnings_within_days("SOXL", days=14) is False


def test_non_etf_still_queries_yfinance(monkeypatch):
    called = {"n": 0}

    def _fetch(_symbol):
        called["n"] += 1
        return None

    monkeypatch.setattr(earnings_calendar, "_yf_next_earnings_date", _fetch)
    earnings_calendar.clear_cache()
    earnings_calendar.next_earnings_date("AAPL")
    assert called["n"] == 1  # a real stock is NOT short-circuited


# ---------------------------------------------------------------------------
# yfinance benign-ERROR downgrade filter.
# ---------------------------------------------------------------------------


def _record(msg: str, level: int = logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord(
        name="yfinance", level=level, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )


def test_benign_earnings_error_downgraded_to_warning():
    f = _YFinanceBenignFilter()
    rec = _record("SOXL: No earnings dates found, symbol may be delisted")
    assert f.filter(rec) is True  # never drops the record
    assert rec.levelno == logging.WARNING
    assert rec.levelname == "WARNING"


def test_benign_fundamentals_error_downgraded():
    f = _YFinanceBenignFilter()
    rec = _record('No fundamentals data found for symbol: SOXL')
    f.filter(rec)
    assert rec.levelno == logging.WARNING


def test_genuine_error_left_at_error():
    f = _YFinanceBenignFilter()
    rec = _record("Connection reset by peer while fetching quote")
    f.filter(rec)
    assert rec.levelno == logging.ERROR  # real errors are untouched


def test_non_error_levels_untouched():
    f = _YFinanceBenignFilter()
    rec = _record("No earnings dates found", level=logging.INFO)
    f.filter(rec)
    assert rec.levelno == logging.INFO
