"""Tests for the daily earnings tracker (Feature 2)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from config.settings import Settings
from data.earnings_tracker import (
    DailyEarnings,
    record_result,
    sector_contagion,
    symbol_history,
    todays_earnings,
)


@dataclass
class _Entry:
    symbol: str
    earnings_date: Optional[date]
    days_until: Optional[int]
    is_upcoming: bool = True


@dataclass
class _Result:
    eps_estimate: Optional[float]
    eps_actual: Optional[float]
    eps_surprise_pct: Optional[float]
    verdict: str


@dataclass
class _Quote:
    gap_pct: Optional[float]
    session: str = "post"


def _settings(**kw) -> Settings:
    return Settings(**kw)


# ── todays_earnings ─────────────────────────────────────────────────────────


def test_todays_earnings_filters_to_today_and_enriches():
    symbols = ["NVDA", "AAPL", "MSFT"]
    cal = [
        _Entry("NVDA", date(2026, 7, 10), 0),
        _Entry("AAPL", date(2026, 7, 15), 5),   # not today
        _Entry("MSFT", date(2026, 7, 10), 0),
    ]
    results = {
        "NVDA": _Result(1.0, 1.5, 50.0, "beat"),
        "MSFT": _Result(2.0, 1.8, -10.0, "miss"),
    }
    quotes = {"NVDA": _Quote(0.08), "MSFT": _Quote(-0.04)}

    reporters = todays_earnings(
        symbols,
        _settings(),
        calendar_fetcher=lambda s: cal,
        result_fetcher=lambda sym: results.get(sym),
        ext_fetcher=lambda sym: quotes.get(sym),
        today=date(2026, 7, 10),
    )
    syms = [d.symbol for d in reporters]
    assert "AAPL" not in syms
    assert set(syms) == {"NVDA", "MSFT"}
    nvda = next(d for d in reporters if d.symbol == "NVDA")
    assert nvda.verdict == "beat" and nvda.surprise_pct == 50.0
    assert nvda.move_pct == 0.08 and nvda.move_session == "post"
    # Sorted by surprise magnitude: NVDA (50) before MSFT (10).
    assert syms[0] == "NVDA"


def test_todays_earnings_fail_open_on_calendar_error():
    def boom(_s):
        raise RuntimeError("calendar down")

    assert todays_earnings(["NVDA"], _settings(), calendar_fetcher=boom) == []


# ── sector_contagion ────────────────────────────────────────────────────────


def test_sector_contagion_flags_peers_on_big_surprise():
    reported = DailyEarnings("NVDA", "Technology", surprise_pct=12.0, verdict="beat")
    peers = sector_contagion(reported, ["NVDA", "AMD", "MSFT", "JPM"],
                             _settings(CONTAGION_SURPRISE_THRESHOLD=5.0))
    # AMD/MSFT are Technology; JPM is Financials.
    assert "AMD" in peers and "MSFT" in peers and "JPM" not in peers
    assert "NVDA" not in peers  # never flags itself


def test_sector_contagion_quiet_surprise_no_peers():
    reported = DailyEarnings("NVDA", "Technology", surprise_pct=1.0, verdict="inline")
    assert sector_contagion(reported, ["NVDA", "AMD"], _settings()) == []


def test_sector_contagion_unknown_sector():
    reported = DailyEarnings("ZZZZ", "Unknown", surprise_pct=20.0, verdict="beat")
    assert sector_contagion(reported, ["ZZZZ", "AMD"], _settings()) == []


# ── history store ───────────────────────────────────────────────────────────


def test_history_record_and_read(tmp_data_dir):
    e1 = DailyEarnings("NVDA", "Technology", earnings_date=date(2026, 4, 1),
                       verdict="beat", surprise_pct=10.0)
    e2 = DailyEarnings("NVDA", "Technology", earnings_date=date(2026, 7, 10),
                       verdict="miss", surprise_pct=-5.0)
    record_result(tmp_data_dir, e1)
    record_result(tmp_data_dir, e2)
    hist = symbol_history(tmp_data_dir, "NVDA")
    assert len(hist) == 2
    # Newest first.
    assert hist[0]["earnings_date"] == "2026-07-10"


def test_history_dedupes_same_key(tmp_data_dir):
    e = DailyEarnings("AAPL", "Technology", earnings_date=date(2026, 7, 10),
                      verdict="beat", surprise_pct=3.0)
    record_result(tmp_data_dir, e)
    record_result(tmp_data_dir, e)  # same (symbol, date) key
    assert len(symbol_history(tmp_data_dir, "AAPL")) == 1
