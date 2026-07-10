"""Tests for the earnings block/flag entry gate (Feature 1a)."""

from __future__ import annotations

from datetime import date

from config.settings import Settings
from signals.earnings_filter import EarningsEntryFilter
from signals.signal_types import Grade, Signal


def _sig(symbol: str = "AAPL", strategy: str = "momentum") -> Signal:
    return Signal(
        symbol=symbol,
        strategy=strategy,
        direction="long",
        entry_price=100.0,
        stop_price=97.0,
        target_price=106.0,
        signal_strength=0.8,
        grade=Grade.A,
    )


_TODAY = date(2026, 7, 10)


def _filter(mode: str, edate, block_days: int = 2) -> EarningsEntryFilter:
    settings = Settings(EARNINGS_FILTER_MODE=mode, EARNINGS_BLOCK_DAYS=block_days)
    return EarningsEntryFilter(
        settings, date_fetcher=lambda sym: edate, today=lambda: _TODAY
    )


def test_off_mode_always_allows():
    f = _filter("off", date(2026, 7, 11))
    r = f.check(_sig())
    assert r.allowed and r.mode == "ok"


def test_block_mode_rejects_within_window():
    f = _filter("block", date(2026, 7, 11))  # 1 day out
    r = f.check(_sig())
    assert not r.allowed and r.mode == "block"
    assert r.days_until == 1


def test_block_mode_allows_outside_window():
    f = _filter("block", date(2026, 7, 20))  # 10 days out
    r = f.check(_sig())
    assert r.allowed and r.mode == "ok"


def test_flag_mode_annotates_but_allows():
    f = _filter("flag", date(2026, 7, 11))
    sig = _sig()
    r = f.check(sig)
    assert r.allowed and r.mode == "flag"
    assert sig.raw_data.get("earnings_flag", {}).get("days_until") == 1


def test_pead_is_exempt_from_block():
    f = _filter("block", date(2026, 7, 11))
    r = f.check(_sig(strategy="pead"))
    assert r.allowed and r.mode == "ok"


def test_missing_date_fails_open():
    f = _filter("block", None)
    r = f.check(_sig())
    assert r.allowed and r.mode == "ok"


def test_lookup_error_fails_open():
    settings = Settings(EARNINGS_FILTER_MODE="block")

    def boom(_sym):
        raise RuntimeError("calendar down")

    f = EarningsEntryFilter(settings, date_fetcher=boom, today=lambda: _TODAY)
    r = f.check(_sig())
    assert r.allowed and r.mode == "ok"


def test_past_earnings_not_blocked():
    # A date in the past (negative days_until) is not an upcoming-entry risk.
    f = _filter("block", date(2026, 7, 8))
    r = f.check(_sig())
    assert r.allowed and r.mode == "ok"
