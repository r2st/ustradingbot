"""Tests for earnings-results tracking (Feature 1b) and beat-aware PEAD (1c)."""

from __future__ import annotations

from config.settings import Settings
from data.earnings import (
    EarningsResult,
    clear_result_cache,
    get_earnings_result,
    _parse_earnings_rows,
    _verdict_from_surprise,
)


def setup_function(_fn):
    clear_result_cache()


# ── verdict classification ──────────────────────────────────────────────────


def test_verdict_bands():
    assert _verdict_from_surprise(10.0) == "beat"
    assert _verdict_from_surprise(-10.0) == "miss"
    assert _verdict_from_surprise(1.0) == "inline"
    assert _verdict_from_surprise(None) == "inline"


# ── row parsing ─────────────────────────────────────────────────────────────


def test_parse_picks_latest_period_and_beat():
    rows = [
        {"actual": 1.0, "estimate": 1.0, "surprisePercent": 0.0, "period": "2026-01-01"},
        {"actual": 1.5, "estimate": 1.2, "surprisePercent": 25.0, "period": "2026-04-01"},
    ]
    r = _parse_earnings_rows(rows)
    assert r is not None
    assert r.verdict == "beat"
    assert r.eps_actual == 1.5
    assert str(r.report_date) == "2026-04-01"


def test_parse_computes_surprise_when_missing():
    rows = [{"actual": 1.1, "estimate": 1.0, "period": "2026-04-01"}]
    r = _parse_earnings_rows(rows)
    assert r is not None and r.eps_surprise_pct == 10.0
    assert r.verdict == "beat"


def test_parse_ignores_unreported_rows():
    rows = [{"actual": None, "estimate": 2.0, "period": "2026-07-01"}]
    assert _parse_earnings_rows(rows) is None


def test_parse_revenue_surprise():
    rows = [{
        "actual": 2.0, "estimate": 1.9, "period": "2026-04-01",
        "revenueActual": 110.0, "revenueEstimate": 100.0,
    }]
    r = _parse_earnings_rows(rows)
    assert r is not None and r.rev_surprise_pct == 10.0


# ── get_earnings_result (injected fetcher) ──────────────────────────────────


def test_get_earnings_result_with_fetcher():
    settings = Settings(FINNHUB_API_KEY="x")
    rows = [{"actual": 2.0, "estimate": 1.5, "surprisePercent": 33.3, "period": "2026-04-01"}]
    r = get_earnings_result("AAPL", settings, fetcher=lambda s: rows)
    assert isinstance(r, EarningsResult) and r.verdict == "beat"


def test_get_earnings_result_fail_open_on_error():
    settings = Settings(FINNHUB_API_KEY="x")

    def boom(_s):
        raise RuntimeError("finnhub down")

    assert get_earnings_result("AAPL", settings, fetcher=boom) is None


def test_get_earnings_result_no_key_returns_none():
    # Default (live) fetch path with no key -> None, no network.
    assert get_earnings_result("AAPL", Settings(FINNHUB_API_KEY="")) is None


def test_get_earnings_result_caches():
    settings = Settings(FINNHUB_API_KEY="x")
    calls = {"n": 0}

    def fetch(_s):
        calls["n"] += 1
        return [{"actual": 1.0, "estimate": 0.9, "period": "2026-04-01"}]

    get_earnings_result("MSFT", settings, fetcher=fetch)
    get_earnings_result("MSFT", settings, fetcher=fetch)
    assert calls["n"] == 1  # second call served from cache
