"""Tests for the MA-crossover / volume signal pages, watchlist and price-alert landing."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from dashboard import public_market_router
from dashboard.app import app


def _bars(start: float, step: float, n: int = 60, volume: int = 1_000_000) -> pd.DataFrame:
    close = np.array([start + step * i for i in range(n)], dtype=float)
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": volume},
        index=idx,
    )


@pytest.fixture(autouse=True)
def _fake_market(monkeypatch: pytest.MonkeyPatch):
    """Rising bars everywhere except NVDA (falling) and AAPL (volume spike)."""

    def fake(symbol: str):
        if symbol == "NVDA":
            return _bars(200.0, -1.0)
        if symbol == "AAPL":
            df = _bars(100.0, 0.5)
            df.iloc[-1, df.columns.get_loc("Volume")] = 3_000_000
            return df
        return _bars(100.0, 0.5)

    monkeypatch.setattr(public_market_router, "_fetch_ohlcv", fake)
    public_market_router.clear_cache()
    yield
    public_market_router.clear_cache()


client = TestClient(app, raise_server_exceptions=False)


class TestMaCross:
    def test_rising_series_is_golden(self):
        row = public_market_router._symbol_row("MSFT")
        assert row["ma_state"] == "golden"
        assert row["ma_cross_days"] is None  # never flipped in the window

    def test_falling_series_is_death(self):
        row = public_market_router._symbol_row("NVDA")
        assert row["ma_state"] == "death"

    def test_needs_history(self):
        state, days = public_market_router._ma_cross(pd.Series(range(30), dtype=float))
        assert state is None and days is None

    def test_cross_days_counts_the_flip(self):
        # 40 falling bars then 40 strongly rising: the 20-day SMA overtakes the 50-day.
        close = pd.Series(
            [200.0 - i for i in range(40)] + [160.0 + 4.0 * i for i in range(40)]
        )
        state, days = public_market_router._ma_cross(close)
        assert state == "golden"
        assert days is not None and 0 < days < 30


class TestVolumeStats:
    def test_constant_volume_ratio_is_one(self):
        row = public_market_router._symbol_row("MSFT")
        assert row["vol_ratio"] == 1.0
        assert row["avg_volume"] == 1_000_000

    def test_spike_shows_up(self):
        row = public_market_router._symbol_row("AAPL")
        assert row["vol_ratio"] == 3.0

    def test_missing_volume_is_none(self):
        df = _bars(100.0, 0.5).drop(columns=["Volume"])
        assert public_market_router._volume_stats(df) == (None, None, None)


class TestSignalPages:
    def test_ma_crossover_page_renders(self):
        r = client.get("/signals/ma-crossover")
        assert r.status_code == 200
        assert "Moving Average Crossover Signals" in r.text
        assert "golden" in r.text

    def test_volume_page_renders(self):
        r = client.get("/signals/volume")
        assert r.status_code == 200
        assert "Unusual Volume Scanner" in r.text

    def test_ma_crossover_api(self):
        r = client.get("/api/public/signals/ma-crossover")
        assert r.status_code == 200
        rows = {row["symbol"]: row for row in r.json()["rows"]}
        assert rows["NVDA"]["ma_state"] == "death"
        assert rows["MSFT"]["ma_state"] == "golden"

    def test_volume_api_sorted_by_ratio(self):
        r = client.get("/api/public/signals/volume")
        assert r.status_code == 200
        rows = r.json()["rows"]
        assert rows[0]["symbol"] == "AAPL"
        assert rows[0]["vol_ratio"] == 3.0

    def test_no_auth_required(self):
        for path in ("/signals/ma-crossover", "/signals/volume",
                     "/api/public/signals/ma-crossover", "/api/public/signals/volume"):
            assert client.get(path).status_code != 401, path


class TestWatchlistAndAlerts:
    def test_watchlist_page_renders(self):
        r = client.get("/watchlist")
        assert r.status_code == 200
        assert "My Watchlist" in r.text
        assert "/static/watchlist.js" in r.text

    def test_price_alerts_landing_renders(self):
        r = client.get("/price-alerts")
        assert r.status_code == 200
        assert "Stock Price Alerts" in r.text
        assert "application/ld+json" in r.text

    def test_screener_has_star_column(self):
        r = client.get("/screener")
        assert "/static/watchlist.js" in r.text
        assert "DoAideWatchlist" in r.text

    def test_stock_page_has_watchlist_button(self):
        r = client.get("/stock/AAPL")
        assert 'id="wl-btn"' in r.text


class TestSeo:
    def test_new_pages_in_sitemap(self):
        xml = client.get("/sitemap.xml").text
        for path in ("/signals/ma-crossover", "/signals/volume", "/watchlist",
                     "/price-alerts", "/blog/best-free-stock-screeners-india-2026",
                     "/blog/how-to-use-rsi-for-trading"):
            assert f"https://trade.doaide.com{path}" in xml, path

    def test_new_blog_posts_render(self):
        r = client.get("/blog/best-free-stock-screeners-india-2026")
        assert r.status_code == 200
        assert "Screener.in" in r.text
        r = client.get("/blog/how-to-use-rsi-for-trading")
        assert r.status_code == 200
        assert "Relative Strength Index" in r.text

    def test_nav_links_watchlist(self):
        assert 'href="/watchlist"' in client.get("/tools").text
