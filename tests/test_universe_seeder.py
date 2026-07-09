"""Tests for data_store.universe_seeder — SIC mapping, stock filters, seeder."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from data_store.universe_seeder import (
    UniverseSeeder,
    _is_common_stock,
    sic_to_gics,
    CA_TICKERS,
    MAJOR_ETFS,
)
from data_store.universe import UniverseDB


# ---------------------------------------------------------------------------
# sic_to_gics
# ---------------------------------------------------------------------------


class TestSicToGics:
    def test_energy(self) -> None:
        assert sic_to_gics(1000) == "Energy"
        assert sic_to_gics(1200) == "Energy"

    def test_technology(self) -> None:
        assert sic_to_gics(3600) == "Information Technology"
        assert sic_to_gics(7300) == "Information Technology"

    def test_health_care(self) -> None:
        assert sic_to_gics(2800) == "Health Care"
        assert sic_to_gics(8000) == "Health Care"

    def test_financials(self) -> None:
        assert sic_to_gics(6000) == "Financials"
        assert sic_to_gics(6300) == "Financials"

    def test_consumer_staples(self) -> None:
        assert sic_to_gics(2000) == "Consumer Staples"

    def test_industrials(self) -> None:
        assert sic_to_gics(1500) == "Industrials"

    def test_real_estate(self) -> None:
        assert sic_to_gics(6500) == "Real Estate"

    def test_utilities_for_90s(self) -> None:
        assert sic_to_gics(9100) == "Utilities"

    def test_zero_returns_unknown(self) -> None:
        assert sic_to_gics(0) == "Unknown"

    def test_handles_raw_two_digit(self) -> None:
        # Division code directly (e.g. 73 without trailing zeros)
        assert sic_to_gics(73) == "Information Technology"


# ---------------------------------------------------------------------------
# _is_common_stock
# ---------------------------------------------------------------------------


class TestIsCommonStock:
    @pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "GOOG", "AMZN", "JPM", "SHOP", "LOW", "NOW"])
    def test_real_stocks_pass(self, ticker: str) -> None:
        assert _is_common_stock(ticker) is True

    @pytest.mark.parametrize("ticker", [
        "XYZ.WS", "ABC.WT", "DEF.PR", "GHI.PRA",
        "JKL.UN", "MNO.RT", "PQR.R", "STU.U", "VWX.W",
    ])
    def test_non_common_filtered(self, ticker: str) -> None:
        assert _is_common_stock(ticker) is False

    def test_empty_string(self) -> None:
        assert _is_common_stock("") is False

    def test_too_long(self) -> None:
        assert _is_common_stock("ABCDEFGHIJK") is False


# ---------------------------------------------------------------------------
# UniverseSeeder
# ---------------------------------------------------------------------------


@pytest.fixture
def seeder(tmp_path: Path) -> UniverseSeeder:
    """Create a seeder backed by a temp database."""
    return UniverseSeeder(str(tmp_path / "universe.db"))


class TestSeederCanadian:
    def test_seed_canadian_adds_symbols(self, seeder: UniverseSeeder) -> None:
        added = seeder.seed_canadian()
        assert added == len(CA_TICKERS)
        rows = seeder.db.get_symbols(country="CA")
        tickers = {r["ticker"] for r in rows}
        assert "SHOP.TO" in tickers
        assert "RY.TO" in tickers

    def test_seed_canadian_sets_exchange(self, seeder: UniverseSeeder) -> None:
        seeder.seed_canadian()
        rows = seeder.db.get_symbols(exchange="TSX")
        assert len(rows) > 0


class TestSeederETFs:
    def test_seed_etfs_adds_symbols(self, seeder: UniverseSeeder) -> None:
        added = seeder.seed_etfs()
        assert added == len(MAJOR_ETFS)

    def test_etfs_have_correct_asset_type(self, seeder: UniverseSeeder) -> None:
        seeder.seed_etfs()
        rows = seeder.db.get_symbols(asset_type="etf")
        assert len(rows) == len(MAJOR_ETFS)

    def test_canadian_etf_currency(self, seeder: UniverseSeeder) -> None:
        seeder.seed_etfs()
        rows = seeder.db.get_symbols(search="XIU")
        assert rows[0]["currency"] == "CAD"
        assert rows[0]["country"] == "CA"


class TestSeederSECFetch:
    def test_fetch_sec_tickers_with_mock(self, seeder: UniverseSeeder) -> None:
        """Mock the HTTP call to SEC EDGAR."""
        import httpx as _httpx

        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc", "sic": 3674},
            "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp", "sic": 7372},
            "2": {"cik_str": 123456, "ticker": "XYZ.WS", "title": "Bad Warrant", "sic": 6000},
        }
        fake_response.raise_for_status = MagicMock()

        with patch.object(_httpx, "get", return_value=fake_response):
            symbols = seeder.fetch_sec_tickers()

        tickers = {s["ticker"] for s in symbols}
        assert "AAPL" in tickers
        assert "MSFT" in tickers
        assert "XYZ.WS" not in tickers  # filtered out by _is_common_stock

        # Check sector mapping
        aapl = next(s for s in symbols if s["ticker"] == "AAPL")
        assert aapl["sector"] == "Information Technology"  # SIC 3674

    def test_fetch_sec_tickers_network_error(self, seeder: UniverseSeeder) -> None:
        """Network errors return empty list, don't raise."""
        import httpx as _httpx

        with patch.object(_httpx, "get", side_effect=Exception("timeout")):
            symbols = seeder.fetch_sec_tickers()
        assert symbols == []


class TestSeederDefaultFilters:
    def test_seed_default_filters(self, seeder: UniverseSeeder) -> None:
        seeder.seed_default_filters()
        filters = seeder.db.get_scan_filters()
        assert filters["min_price"] == 5.0
        assert filters["min_volume"] == 100_000
        assert filters["min_market_cap"] == 300_000_000


class TestSeederMigrateWatchlist:
    def test_migrate_creates_watchlists(self, seeder: UniverseSeeder) -> None:
        seeder.migrate_existing_watchlist()
        active = seeder.db.get_active_watchlist()
        # Should contain all 41 legacy symbols
        assert len(active) > 0
        # Check specific symbols
        assert "AAPL" in active
        assert "MSFT" in active

    def test_migrate_creates_sector_lists(self, seeder: UniverseSeeder) -> None:
        seeder.migrate_existing_watchlist()
        names = seeder.db.get_watchlist_names()
        list_names = {n["list_name"] for n in names}
        assert "Active Watchlist" in list_names
        assert "Technology" in list_names


class TestSeederSeedAll:
    def test_seed_all_skip_enrichment(self, seeder: UniverseSeeder) -> None:
        """seed_all with skip_enrichment and mocked SEC fetch."""
        import httpx as _httpx

        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc", "sic": 3674},
            "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp", "sic": 7372},
        }
        fake_response.raise_for_status = MagicMock()

        with patch.object(_httpx, "get", return_value=fake_response):
            stats = seeder.seed_all(skip_enrichment=True)

        assert stats["sec_fetched"] == 2
        assert stats["enriched"] == 0
        assert stats["ca_added"] == len(CA_TICKERS)
        assert stats["etf_added"] == len(MAJOR_ETFS)
        assert stats["filters_set"] is True
        assert stats["watchlist_migrated"] is True
        assert stats["total_symbols"] > 0
