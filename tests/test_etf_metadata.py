"""Tests for live ETF metadata: quoteType detection + fund fundamentals (Gaps 1 & 2)."""

from __future__ import annotations

import pytest

from config.settings import Settings
from data import etf_metadata
from data.etf_metadata import (
    ETFInfo,
    detect_quote_type,
    get_etf_info,
    is_etf_via_yfinance,
)


# ── Fakes ────────────────────────────────────────────────────────────────────


class _FundsData:
    def __init__(self, top_holdings):
        self.top_holdings = top_holdings


class FakeTicker:
    """Minimal stand-in for ``yfinance.Ticker`` used across the metadata tests."""

    def __init__(self, info=None, top_holdings=None, raise_on_info=False):
        self._info = info or {}
        self._raise = raise_on_info
        self.funds_data = _FundsData(top_holdings) if top_holdings is not None else None

    @property
    def info(self):
        if self._raise:
            raise RuntimeError("network down")
        return self._info


@pytest.fixture
def offline_settings(tmp_data_dir):
    return Settings(DATA_DIR=tmp_data_dir)


def _install(monkeypatch, factory):
    monkeypatch.setattr(etf_metadata, "_ticker_factory", factory)
    etf_metadata.clear_cache()


# A steadily incrementing fake clock for TTL tests.
class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


# ── quoteType detection (Gap 1) ──────────────────────────────────────────────


def test_detect_quote_type_etf(monkeypatch, offline_settings):
    _install(monkeypatch, lambda s: FakeTicker(info={"quoteType": "ETF"}))
    assert detect_quote_type("VTI", settings=offline_settings) == "ETF"
    assert is_etf_via_yfinance("VTI", settings=offline_settings) is True


def test_detect_quote_type_equity(monkeypatch, offline_settings):
    _install(monkeypatch, lambda s: FakeTicker(info={"quoteType": "EQUITY"}))
    assert detect_quote_type("AAPL", settings=offline_settings) == "EQUITY"
    assert is_etf_via_yfinance("AAPL", settings=offline_settings) is False


def test_detect_quote_type_failopen(monkeypatch, offline_settings):
    _install(monkeypatch, lambda s: FakeTicker(raise_on_info=True))
    assert detect_quote_type("XYZ", settings=offline_settings) is None
    assert is_etf_via_yfinance("XYZ", settings=offline_settings) is None


def test_detect_quote_type_disabled(monkeypatch, tmp_data_dir):
    s = Settings(DATA_DIR=tmp_data_dir, ETF_DYNAMIC_DETECTION=False)
    calls = {"n": 0}

    def factory(sym):
        calls["n"] += 1
        return FakeTicker(info={"quoteType": "ETF"})

    _install(monkeypatch, factory)
    assert detect_quote_type("VTI", settings=s) is None
    assert calls["n"] == 0  # never touched the network


def test_detect_quote_type_is_cached(monkeypatch, offline_settings):
    calls = {"n": 0}

    def factory(sym):
        calls["n"] += 1
        return FakeTicker(info={"quoteType": "ETF"})

    _install(monkeypatch, factory)
    clock = _Clock()
    assert detect_quote_type("VTI", settings=offline_settings, clock=clock) == "ETF"
    assert detect_quote_type("VTI", settings=offline_settings, clock=clock) == "ETF"
    assert calls["n"] == 1  # second call served from cache


def test_detect_quote_type_cache_expires(monkeypatch, tmp_data_dir):
    s = Settings(DATA_DIR=tmp_data_dir, ETF_DETECT_TTL_MINUTES=1.0)
    calls = {"n": 0}

    def factory(sym):
        calls["n"] += 1
        return FakeTicker(info={"quoteType": "ETF"})

    _install(monkeypatch, factory)
    clock = _Clock()
    detect_quote_type("VTI", settings=s, clock=clock)
    clock.t = 61.0  # past the 1-minute TTL
    detect_quote_type("VTI", settings=s, clock=clock)
    assert calls["n"] == 2


# ── fund fundamentals (Gap 2) ────────────────────────────────────────────────


def test_get_etf_info_full(monkeypatch, offline_settings):
    info = {
        "quoteType": "ETF",
        "annualReportExpenseRatio": 0.0003,
        "navPrice": 245.12,
        "category": "Large Blend",
        "fundFamily": "Vanguard",
        "longName": "Vanguard Total Stock Market ETF",
    }
    holdings = [
        {"symbol": "AAPL", "name": "Apple Inc", "pct": 0.071},
        {"symbol": "MSFT", "name": "Microsoft", "pct": 0.065},
    ]
    _install(monkeypatch, lambda s: FakeTicker(info=info, top_holdings=holdings))

    etf = get_etf_info("VTI", settings=offline_settings)
    assert isinstance(etf, ETFInfo)
    assert etf.expense_ratio == pytest.approx(0.0003)
    assert etf.expense_ratio_pct == pytest.approx(0.03)
    assert etf.nav_price == pytest.approx(245.12)
    assert etf.category == "Large Blend"
    assert etf.fund_family == "Vanguard"
    assert etf.leverage == "regular"
    assert len(etf.top_holdings) == 2
    assert etf.top_holdings[0]["symbol"] == "AAPL"
    assert etf.top_holdings[0]["pct"] == pytest.approx(7.1)  # rendered as percent

    d = etf.to_dict()
    assert d["is_leveraged"] is False
    assert d["expense_ratio_pct"] == pytest.approx(0.03)


def test_get_etf_info_leveraged(monkeypatch, offline_settings):
    info = {
        "quoteType": "ETF",
        "category": "Trading--Leveraged Equity",
        "fundFamily": "ProShares",
        "longName": "ProShares UltraPro QQQ",
    }
    _install(monkeypatch, lambda s: FakeTicker(info=info))
    etf = get_etf_info("TQQQ", settings=offline_settings)
    assert etf is not None
    assert etf.leverage == "leveraged_3x"
    assert etf.leverage_label == "3x Leveraged"
    assert etf.to_dict()["is_leveraged"] is True


def test_get_etf_info_truncates_to_ten_holdings(monkeypatch, offline_settings):
    holdings = [{"symbol": f"S{i}", "name": f"n{i}", "pct": 0.01} for i in range(25)]
    _install(
        monkeypatch,
        lambda s: FakeTicker(info={"category": "Blend"}, top_holdings=holdings),
    )
    etf = get_etf_info("XXX", settings=offline_settings)
    assert etf is not None
    assert len(etf.top_holdings) == 10


def test_get_etf_info_failopen(monkeypatch, offline_settings):
    _install(monkeypatch, lambda s: FakeTicker(raise_on_info=True))
    assert get_etf_info("BAD", settings=offline_settings) is None


def test_get_etf_info_empty_returns_none(monkeypatch, offline_settings):
    # No usable fields at all → no manufactured card.
    _install(monkeypatch, lambda s: FakeTicker(info={"quoteType": "ETF"}))
    assert get_etf_info("EMPTY", settings=offline_settings) is None


def test_get_etf_info_decimal_expense_passthrough(monkeypatch, offline_settings):
    # A normal decimal expense ratio (0.0009 == 0.09 %) passes through unchanged.
    _install(
        monkeypatch,
        lambda s: FakeTicker(info={"expenseRatio": 0.0009, "category": "Bonds"}),
    )
    etf = get_etf_info("BND", settings=offline_settings)
    assert etf is not None
    assert etf.expense_ratio == pytest.approx(0.0009)


def test_get_etf_info_percent_expense_normalised(monkeypatch, offline_settings):
    # A feed reporting an implausible >1 value (1.5 meaning 1.5 %) is scaled down.
    _install(
        monkeypatch,
        lambda s: FakeTicker(info={"expenseRatio": 1.5, "category": "Bonds"}),
    )
    etf = get_etf_info("BND", settings=offline_settings)
    assert etf is not None
    assert etf.expense_ratio == pytest.approx(0.015)
