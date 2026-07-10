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
        assert data["tier1_count"] == 2
        assert "tier2_sectors" in data
        assert data["tier3_count"] >= 0


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
