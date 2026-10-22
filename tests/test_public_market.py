"""Tests for the login-free market overview and stock screener."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from dashboard import public_market_router
from dashboard.app import app


def _bars(start: float, step: float, n: int = 60) -> pd.DataFrame:
    close = np.array([start + step * i for i in range(n)], dtype=float)
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1_000_000},
        index=idx,
    )


@pytest.fixture(autouse=True)
def _fake_market(monkeypatch: pytest.MonkeyPatch):
    """Rising bars for every symbol except NVDA (falling) and BAD (fetch error)."""

    def fake(symbol: str):
        if symbol == "BAD":
            raise RuntimeError("provider down")
        if symbol == "NVDA":
            return _bars(200.0, -1.0)
        return _bars(100.0, 0.5)

    monkeypatch.setattr(public_market_router, "_fetch_ohlcv", fake)
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "True")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    public_market_router.clear_cache()
    yield
    public_market_router.clear_cache()


client = TestClient(app, raise_server_exceptions=False)


class TestSymbolRow:
    def test_rising_series_metrics(self):
        row = public_market_router._symbol_row("AAPL")
        assert row["price"] == pytest.approx(129.5)
        assert row["change_pct"] > 0
        assert row["month_pct"] > 0
        assert row["above_sma50"] is True
        assert row["rsi"] == 100.0
        assert row["off_high_pct"] == 0.0

    def test_falling_series_below_trend(self):
        row = public_market_router._symbol_row("NVDA")
        assert row["change_pct"] < 0
        assert row["above_sma50"] is False
        assert row["rsi"] < 30

    def test_failed_fetch_is_skipped(self):
        assert public_market_router._symbol_row("BAD") is None
        assert public_market_router._rows(["AAPL", "BAD"])[0]["symbol"] == "AAPL"
        assert len(public_market_router._rows(["AAPL", "BAD"])) == 1

    def test_rsi_needs_history(self):
        assert public_market_router._rsi(pd.Series([1.0, 2.0, 3.0])) is None


class TestPublicApi:
    def test_screener_api_no_auth(self):
        r = client.get("/api/public/screener")
        assert r.status_code == 200
        body = r.json()
        symbols = {row["symbol"] for row in body["rows"]}
        assert "AAPL" in symbols and "NVDA" in symbols
        assert body["sectors"]

    def test_market_api_no_auth(self):
        r = client.get("/api/public/market")
        assert r.status_code == 200
        body = r.json()
        assert {row["symbol"] for row in body["indices"]} == {"SPY", "QQQ", "IWM", "DIA"}
        assert body["indices"][0]["name"]
        assert len(body["sectors"]) == 11
        assert body["losers"][0]["symbol"] == "NVDA"
        assert body["breadth"]["decliners"] == 1

    def test_snapshot_is_cached(self, monkeypatch: pytest.MonkeyPatch):
        client.get("/api/public/screener")
        calls = []
        monkeypatch.setattr(public_market_router, "_fetch_ohlcv", lambda s: calls.append(s))
        client.get("/api/public/screener")
        assert calls == []


class TestPublicPages:
    def test_market_page_renders_without_login(self):
        r = client.get("/market")
        assert r.status_code == 200
        assert "US Market Overview" in r.text
        assert "S&amp;P 500" in r.text or "S&P 500" in r.text
        assert "Sector performance" in r.text

    def test_screener_page_renders_without_login(self):
        r = client.get("/screener")
        assert r.status_code == 200
        assert "Free Stock Screener" in r.text
        assert "AAPL" in r.text
        assert 'id="f-sector"' in r.text

    def test_pages_degrade_when_data_unavailable(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(public_market_router, "_fetch_ohlcv", lambda s: None)
        for path in ("/market", "/screener"):
            r = client.get(path)
            assert r.status_code == 200
            assert "temporarily unavailable" in r.text

    def test_dashboard_still_requires_login(self):
        r = client.get("/dashboard")
        assert r.status_code == 401

    def test_landing_links_free_pages(self):
        r = client.get("/")
        assert 'href="/screener"' in r.text
        assert 'href="/market"' in r.text
