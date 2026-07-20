"""Tests for dashboard.universe_router — universe API endpoints."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
from config.settings import Settings
from data_store.universe import UniverseDB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _basic_header(user: str = "admin", password: str = "test") -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def tmp_db(tmp_path: Path) -> UniverseDB:
    """Create a temp universe DB seeded with sample data."""
    db = UniverseDB(tmp_path / "universe.db")
    db.add_symbols([
        {"ticker": "AAPL", "name": "Apple Inc.", "exchange": "NASDAQ",
         "asset_type": "STOCK", "sector": "Technology", "last_price": 190.0,
         "avg_volume": 50_000_000, "market_cap": 3e12, "country": "US", "currency": "USD"},
        {"ticker": "MSFT", "name": "Microsoft Corp.", "exchange": "NASDAQ",
         "asset_type": "STOCK", "sector": "Technology", "last_price": 410.0,
         "avg_volume": 25_000_000, "market_cap": 2.8e12, "country": "US", "currency": "USD"},
        {"ticker": "JPM", "name": "JPMorgan Chase", "exchange": "NYSE",
         "asset_type": "STOCK", "sector": "Financials", "last_price": 195.0,
         "avg_volume": 10_000_000, "market_cap": 5e11, "country": "US", "currency": "USD"},
        {"ticker": "SPY", "name": "SPDR S&P 500 ETF", "exchange": "NYSE",
         "asset_type": "ETF", "last_price": 530.0,
         "avg_volume": 80_000_000, "market_cap": 4e11, "country": "US", "currency": "USD"},
    ])
    return db


@pytest.fixture
def client(tmp_path: Path, tmp_db: UniverseDB, monkeypatch) -> TestClient:
    """TestClient with auth disabled and universe DB available."""
    settings = Settings(
        DATA_DIR=tmp_path,
        DASHBOARD_AUTH_ENABLED=False,
    )
    monkeypatch.setattr(dash, "get_settings", lambda: settings)

    # Patch the universe router helpers to use our temp DB
    import dashboard.universe_router as ur
    monkeypatch.setattr(ur, "_settings", lambda: settings)
    monkeypatch.setattr(ur, "_db", lambda: tmp_db)
    monkeypatch.setattr(ur, "_require_db", lambda: tmp_db)

    return TestClient(dash.app, raise_server_exceptions=False)


@pytest.fixture
def client_no_db(tmp_path: Path, monkeypatch) -> TestClient:
    """TestClient where universe DB does not exist."""
    settings = Settings(
        DATA_DIR=tmp_path,
        DASHBOARD_AUTH_ENABLED=False,
    )
    monkeypatch.setattr(dash, "get_settings", lambda: settings)

    import dashboard.universe_router as ur
    monkeypatch.setattr(ur, "_settings", lambda: settings)
    monkeypatch.setattr(ur, "_require_db", lambda: None)

    return TestClient(dash.app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# DB not initialized
# ---------------------------------------------------------------------------


class TestNotInitialized:
    def test_symbols_returns_not_available(self, client_no_db: TestClient) -> None:
        resp = client_no_db.get("/api/universe/symbols")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is False

    def test_sectors_returns_not_available(self, client_no_db: TestClient) -> None:
        resp = client_no_db.get("/api/universe/sectors")
        assert resp.status_code == 200
        assert resp.json()["available"] is False


# ---------------------------------------------------------------------------
# Symbol browsing
# ---------------------------------------------------------------------------


class TestSymbolEndpoints:
    def test_get_symbols(self, client: TestClient) -> None:
        resp = client.get("/api/universe/symbols")
        assert resp.status_code == 200
        data = resp.json()
        assert "symbols" in data
        assert data["total"] == 4

    def test_get_symbols_filter_sector(self, client: TestClient) -> None:
        resp = client.get("/api/universe/symbols?sector=Technology")
        data = resp.json()
        assert data["total"] == 2
        tickers = {s["ticker"] for s in data["symbols"]}
        assert tickers == {"AAPL", "MSFT"}

    def test_get_symbols_pagination(self, client: TestClient) -> None:
        resp = client.get("/api/universe/symbols?limit=2&offset=0")
        data = resp.json()
        assert len(data["symbols"]) == 2
        assert data["total"] == 4

    def test_get_sectors(self, client: TestClient) -> None:
        resp = client.get("/api/universe/sectors")
        data = resp.json()
        assert "sectors" in data
        sectors = {s["sector"] for s in data["sectors"]}
        assert "Technology" in sectors

    def test_get_exchanges(self, client: TestClient) -> None:
        resp = client.get("/api/universe/exchanges")
        data = resp.json()
        assert "exchanges" in data

    def test_get_stats(self, client: TestClient) -> None:
        resp = client.get("/api/universe/stats")
        data = resp.json()
        assert data["total_symbols"] == 4
        assert data["active_symbols"] == 4

    def test_search_symbols(self, client: TestClient) -> None:
        resp = client.get("/api/universe/search?q=Apple")
        data = resp.json()
        assert data["count"] >= 1
        assert any(r["ticker"] == "AAPL" for r in data["results"])

    def test_search_empty_query_400(self, client: TestClient) -> None:
        resp = client.get("/api/universe/search?q=")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Watchlists
# ---------------------------------------------------------------------------


class TestWatchlistEndpoints:
    def test_add_and_get_watchlist(self, client: TestClient, tmp_db: UniverseDB) -> None:
        resp = client.post(
            "/api/universe/watchlists",
            json={"list_name": "Test List", "tickers": ["AAPL", "MSFT"]},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        resp = client.get("/api/universe/watchlists/Test List")
        data = resp.json()
        assert data["count"] == 2

    def test_get_watchlists(self, client: TestClient, tmp_db: UniverseDB) -> None:
        tmp_db.add_to_watchlist("WL1", ["AAPL"])
        resp = client.get("/api/universe/watchlists")
        data = resp.json()
        assert "watchlists" in data

    def test_add_user_symbol_detects_etf_asset_type(
        self, client: TestClient, tmp_db: UniverseDB, monkeypatch
    ) -> None:
        """A user-added ETF not yet in the DB is tagged ``etf`` via quoteType.

        Otherwise the FK-guarded watchlist add would silently drop it (or size
        it as a stock).  VTI is deliberately absent from both the static list
        and the seeded DB, so this exercises the live-quoteType path.
        """
        from data import etf_metadata

        class _T:
            def __init__(self, sym):
                self.sym = sym

            @property
            def info(self):
                return {"quoteType": "ETF" if self.sym == "VTI" else "EQUITY"}

        monkeypatch.setattr(etf_metadata, "_ticker_factory", _T)
        etf_metadata.clear_cache()

        assert tmp_db.get_asset_type("VTI") is None  # unknown beforehand
        resp = client.post(
            "/api/universe/watchlists",
            json={"list_name": "Mine", "tickers": ["VTI"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["created"] == 1
        assert body["added"] == 1
        # The symbol now exists, tagged as an ETF, and is on the watchlist.
        assert tmp_db.get_asset_type("VTI") == "etf"
        assert "VTI" in {r["ticker"] for r in tmp_db.get_watchlist("Mine")}

    def test_add_user_symbol_static_etf_no_network(
        self, client: TestClient, tmp_db: UniverseDB, monkeypatch
    ) -> None:
        """A statically-known ETF is tagged without any yfinance call."""
        from data import etf_metadata

        def _boom(sym):  # pragma: no cover - must never be reached
            raise AssertionError("network hit for a static ETF")

        monkeypatch.setattr(etf_metadata, "_ticker_factory", _boom)
        etf_metadata.clear_cache()

        # QQQ is in the static ETF list but not in the seeded tmp_db.
        resp = client.post(
            "/api/universe/watchlists",
            json={"list_name": "Static", "tickers": ["QQQ"]},
        )
        assert resp.status_code == 200
        assert tmp_db.get_asset_type("QQQ") == "etf"

    def test_delete_watchlist(self, client: TestClient, tmp_db: UniverseDB) -> None:
        tmp_db.add_to_watchlist("ToDelete", ["JPM"])
        resp = client.delete("/api/universe/watchlists/ToDelete")
        assert resp.json()["ok"] is True
        assert tmp_db.get_watchlist("ToDelete") == []

    def test_remove_from_watchlist(self, client: TestClient, tmp_db: UniverseDB) -> None:
        tmp_db.add_to_watchlist("WL", ["AAPL", "MSFT"])
        resp = client.post(
            "/api/universe/watchlists/WL/remove",
            json={"tickers": ["MSFT"]},
        )
        assert resp.json()["ok"] is True
        remaining = tmp_db.get_watchlist("WL")
        assert len(remaining) == 1

    def test_set_watchlist_enabled(self, client: TestClient, tmp_db: UniverseDB) -> None:
        tmp_db.add_to_watchlist("Toggle", ["AAPL"])
        resp = client.post(
            "/api/universe/watchlists/Toggle/enabled",
            json={"enabled": False},
        )
        assert resp.json()["ok"] is True

    def test_add_missing_list_name_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/universe/watchlists",
            json={"tickers": ["AAPL"]},
        )
        assert resp.status_code == 400

    def test_add_empty_tickers_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/universe/watchlists",
            json={"list_name": "X", "tickers": []},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class TestFilterEndpoints:
    def test_get_filters(self, client: TestClient) -> None:
        resp = client.get("/api/universe/filters")
        data = resp.json()
        assert "filters" in data

    def test_set_filter(self, client: TestClient) -> None:
        resp = client.post(
            "/api/universe/filters",
            json={"filter_name": "min_price", "filter_value": 10.0},
        )
        assert resp.json()["ok"] is True

    def test_set_filter_missing_name_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/universe/filters",
            json={"filter_value": 10.0},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------


class TestTierEndpoint:
    def test_get_tiers(self, client: TestClient, tmp_db: UniverseDB) -> None:
        tmp_db.add_to_watchlist("Active", ["AAPL", "MSFT"])
        resp = client.get("/api/universe/tiers")
        data = resp.json()
        assert data["available"] is True
        # Tier 1 = watchlist ∪ ETFs ∪ promoted — the 2 watchlist names are a subset.
        assert data["tier1"]["watchlist_count"] == 2
        assert data["tier1"]["count"] >= 2
        assert data["tier1"]["etf_count"] > 0
        assert data["tier1"]["promoted_count"] == 0
        # Tier 2 = scan pool with the configured pool size and ranking metric.
        assert data["tier2"]["pool_size"] > 0
        assert data["tier2"]["ranking"] == "volume × market cap"
        # Tier 3 = index universe.
        assert data["tier3"]["count"] >= 0
        assert "sp500_count" in data["tier3"]
        assert "nasdaq100_count" in data["tier3"]
        # Settings echoed for the UI.
        assert data["settings"]["TIER2_SCAN_POOL_SIZE"] > 0
        assert data["settings"]["PROMOTION_TTL_HOURS"] >= 0

    def test_get_tiers_reflects_promotion(self, client: TestClient, tmp_db: UniverseDB) -> None:
        tmp_db.promote_symbol("JPM", source_tier="tier2", reason="grade A", ttl_hours=24)
        data = client.get("/api/universe/tiers").json()
        assert data["tier1"]["promoted_count"] == 1
        assert "JPM" in data["tier1"]["symbols"]


class TestIndicesEndpoint:
    def test_get_indices(self, client: TestClient, tmp_db: UniverseDB) -> None:
        tmp_db.set_index_membership("SP500", ["AAPL", "MSFT", "JPM"])
        tmp_db.set_index_membership("NASDAQ100", ["AAPL", "MSFT"])
        data = client.get("/api/universe/indices").json()
        assert data["available"] is True
        assert data["sp500_count"] == 3
        assert data["nasdaq100_count"] == 2
        assert data["both_count"] == 2
        assert data["union_count"] == 3
        by_ticker = {s["ticker"]: s for s in data["symbols"]}
        assert by_ticker["AAPL"]["sp500"] and by_ticker["AAPL"]["nasdaq100"]
        assert by_ticker["JPM"]["sp500"] and not by_ticker["JPM"]["nasdaq100"]

    def test_get_indices_static_fallback(self, client: TestClient) -> None:
        # No recorded membership → falls back to the curated static lists.
        data = client.get("/api/universe/indices").json()
        assert data["sp500_count"] > 0
        assert data["nasdaq100_count"] > 0


class TestScanPoolEndpoint:
    def test_get_scan_pool_ranked(self, client: TestClient, tmp_db: UniverseDB) -> None:
        tmp_db.set_index_membership("SP500", ["AAPL", "MSFT", "JPM"])
        data = client.get("/api/universe/scan-pool?limit=10").json()
        assert data["available"] is True
        assert data["ranking"] == "volume × market cap"
        rows = data["symbols"]
        assert [r["ticker"] for r in rows] == ["AAPL", "MSFT", "JPM"]  # by V×Cap desc
        assert rows[0]["rank"] == 1
        assert rows[0]["liq"] > rows[1]["liq"]


class TestPromotionsEndpoint:
    def test_get_promotions_with_ttl(self, client: TestClient, tmp_db: UniverseDB) -> None:
        tmp_db.promote_symbol("JPM", source_tier="tier3", reason="breakout", ttl_hours=24)
        data = client.get("/api/universe/promotions").json()
        assert data["available"] is True
        assert data["count"] == 1
        promo = data["promotions"][0]
        assert promo["ticker"] == "JPM"
        assert promo["source_tier"] == "tier3"
        assert promo["ttl_remaining_hours"] is not None
        assert 0 < promo["ttl_remaining_hours"] <= 24

    def test_demote_promotion(self, client: TestClient, tmp_db: UniverseDB) -> None:
        tmp_db.promote_symbol("JPM", ttl_hours=24)
        assert client.get("/api/universe/promotions").json()["count"] == 1
        resp = client.post("/api/universe/promotions/JPM/demote")
        assert resp.json()["ok"] is True
        assert client.get("/api/universe/promotions").json()["count"] == 0


# ---------------------------------------------------------------------------
# Seed endpoint
# ---------------------------------------------------------------------------


class TestSeedEndpoint:
    @staticmethod
    def _wait_for_terminal(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
        """Poll the status endpoint until the job leaves the ``running`` state."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = client.get(f"/api/universe/seed/status/{job_id}")
            assert resp.status_code == 200
            data = resp.json()
            if data["state"] != "running":
                return data
            time.sleep(0.02)
        raise AssertionError(f"seed job {job_id} never reached a terminal state")

    def test_seed_returns_job_id(self, client: TestClient) -> None:
        """Seed starts a background job and returns a pollable job_id."""
        with patch("data_store.universe_seeder.UniverseSeeder") as mock_cls:
            seeder = mock_cls.return_value
            seeder.fetch_sec_tickers.return_value = []
            seeder.db.get_stats.return_value = {"total_symbols": 4}
            resp = client.post("/api/universe/seed", json={"skip_enrichment": True})
            assert resp.status_code == 200
            body = resp.json()
            assert body["ok"] is True
            job_id = body["job_id"]
            final = self._wait_for_terminal(client, job_id)

        assert final["state"] == "done"
        assert final["progress"] == 100
        assert "4 symbols" in final["message"]

    def test_seed_status_unknown_job_404(self, client: TestClient) -> None:
        resp = client.get("/api/universe/seed/status/deadbeef")
        assert resp.status_code == 404

    def test_seed_reports_error_state(self, client: TestClient) -> None:
        """A crashing seeder surfaces as an ``error`` job, not a 500."""
        with patch("data_store.universe_seeder.UniverseSeeder") as mock_cls:
            mock_cls.side_effect = RuntimeError("boom")
            resp = client.post("/api/universe/seed", json={"skip_enrichment": True})
            assert resp.status_code == 200
            job_id = resp.json()["job_id"]
            final = self._wait_for_terminal(client, job_id)

        assert final["state"] == "error"
        assert "boom" in final["message"]

    def test_seed_rejects_concurrent_job(self, client: TestClient) -> None:
        """A second seed request while one is running is refused (no job_id)."""
        import threading

        release = threading.Event()

        def _blocking_ctor(*_args, **_kwargs):
            release.wait(timeout=5.0)
            raise RuntimeError("done blocking")

        with patch("data_store.universe_seeder.UniverseSeeder") as mock_cls:
            mock_cls.side_effect = _blocking_ctor
            first = client.post("/api/universe/seed", json={"skip_enrichment": True})
            assert first.json().get("job_id")

            # Second request lands while the first job is still running.
            second = client.post("/api/universe/seed", json={"skip_enrichment": True})
            assert second.json()["ok"] is False
            assert "job_id" not in second.json()

            release.set()
            self._wait_for_terminal(client, first.json()["job_id"])
