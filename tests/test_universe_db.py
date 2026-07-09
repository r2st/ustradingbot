"""Tests for data_store.universe — UniverseDB, get_universe_db, db_exists."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from data_store.universe import UniverseDB, db_exists, get_universe_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sym(
    ticker: str,
    name: str = "",
    exchange: str = "NASDAQ",
    asset_type: str = "STOCK",
    **kwargs,
) -> dict:
    """Shorthand for building a symbol dict with sensible defaults."""
    d: dict = {
        "ticker": ticker,
        "name": name or f"{ticker} Inc.",
        "exchange": exchange,
        "asset_type": asset_type,
    }
    d.update(kwargs)
    return d


SAMPLE_SYMBOLS: list[dict] = [
    _sym("AAPL", "Apple Inc.", sector="Technology", industry="Consumer Electronics",
         market_cap=3_000_000_000_000, avg_volume=50_000_000, last_price=190.0,
         currency="USD", country="US"),
    _sym("MSFT", "Microsoft Corp.", sector="Technology", industry="Software",
         market_cap=2_800_000_000_000, avg_volume=25_000_000, last_price=410.0,
         currency="USD", country="US"),
    _sym("GOOG", "Alphabet Inc.", sector="Technology", industry="Internet",
         market_cap=1_700_000_000_000, avg_volume=20_000_000, last_price=170.0,
         currency="USD", country="US"),
    _sym("JPM", "JPMorgan Chase", exchange="NYSE", sector="Financials",
         industry="Banking", market_cap=500_000_000_000, avg_volume=10_000_000,
         last_price=195.0, currency="USD", country="US"),
    _sym("XOM", "Exxon Mobil", exchange="NYSE", sector="Energy",
         industry="Oil & Gas", market_cap=450_000_000_000, avg_volume=15_000_000,
         last_price=105.0, currency="USD", country="US"),
    _sym("SPY", "SPDR S&P 500 ETF", exchange="NYSE", asset_type="ETF",
         market_cap=400_000_000_000, avg_volume=80_000_000, last_price=530.0,
         currency="USD", country="US"),
    _sym("SHOP", "Shopify Inc.", exchange="TSX", sector="Technology",
         industry="E-Commerce", market_cap=90_000_000_000, avg_volume=5_000_000,
         last_price=85.0, currency="CAD", country="CA"),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def universe_db(tmp_path: Path) -> UniverseDB:
    """Create a fresh UniverseDB backed by a temp directory."""
    return UniverseDB(tmp_path / "universe.db")


@pytest.fixture
def seeded_db(universe_db: UniverseDB) -> UniverseDB:
    """UniverseDB pre-loaded with SAMPLE_SYMBOLS."""
    universe_db.add_symbols(SAMPLE_SYMBOLS)
    return universe_db


# ---------------------------------------------------------------------------
# Module-level helpers: db_exists / get_universe_db
# ---------------------------------------------------------------------------

class TestDbExists:
    def test_false_when_no_file(self, tmp_path: Path) -> None:
        assert db_exists(tmp_path) is False

    def test_true_after_creation(self, tmp_path: Path) -> None:
        UniverseDB(tmp_path / "universe.db")
        assert db_exists(tmp_path) is True


class TestGetUniverseDb:
    def test_returns_universe_db_instance(self, tmp_path: Path) -> None:
        db = get_universe_db(tmp_path)
        assert isinstance(db, UniverseDB)

    def test_singleton_same_dir(self, tmp_path: Path) -> None:
        db1 = get_universe_db(tmp_path)
        db2 = get_universe_db(tmp_path)
        assert db1 is db2


# ---------------------------------------------------------------------------
# Default scan filters
# ---------------------------------------------------------------------------

class TestDefaultScanFilters:
    def test_seeded_on_first_creation(self, universe_db: UniverseDB) -> None:
        filters = universe_db.get_scan_filters()
        assert filters == {
            "min_price": 5.0,
            "min_volume": 100_000,
            "min_market_cap": 300_000_000,
        }


# ---------------------------------------------------------------------------
# add_symbols
# ---------------------------------------------------------------------------

class TestAddSymbols:
    def test_returns_count(self, universe_db: UniverseDB) -> None:
        count = universe_db.add_symbols(SAMPLE_SYMBOLS)
        assert count == len(SAMPLE_SYMBOLS)

    def test_empty_list_returns_zero(self, universe_db: UniverseDB) -> None:
        assert universe_db.add_symbols([]) == 0

    def test_upsert_overwrites(self, universe_db: UniverseDB) -> None:
        universe_db.add_symbols([_sym("AAPL", "Apple Old")])
        universe_db.add_symbols([_sym("AAPL", "Apple New")])
        rows = universe_db.get_symbols(search="AAPL")
        assert len(rows) == 1
        assert rows[0]["name"] == "Apple New"

    def test_missing_required_raises(self, universe_db: UniverseDB) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            universe_db.add_symbols([{"ticker": "BAD"}])

    def test_is_active_defaults_to_true(self, universe_db: UniverseDB) -> None:
        universe_db.add_symbols([_sym("ZZZ")])
        rows = universe_db.get_symbols(search="ZZZ")
        assert rows[0]["is_active"] == 1


# ---------------------------------------------------------------------------
# get_symbols
# ---------------------------------------------------------------------------

class TestGetSymbols:
    def test_all_active(self, seeded_db: UniverseDB) -> None:
        rows = seeded_db.get_symbols()
        assert len(rows) == len(SAMPLE_SYMBOLS)

    def test_filter_exchange(self, seeded_db: UniverseDB) -> None:
        rows = seeded_db.get_symbols(exchange="NYSE")
        tickers = {r["ticker"] for r in rows}
        assert tickers == {"JPM", "XOM", "SPY"}

    def test_filter_sector(self, seeded_db: UniverseDB) -> None:
        rows = seeded_db.get_symbols(sector="Technology")
        tickers = {r["ticker"] for r in rows}
        assert "AAPL" in tickers and "MSFT" in tickers and "GOOG" in tickers

    def test_filter_asset_type(self, seeded_db: UniverseDB) -> None:
        rows = seeded_db.get_symbols(asset_type="ETF")
        assert len(rows) == 1
        assert rows[0]["ticker"] == "SPY"

    def test_filter_country(self, seeded_db: UniverseDB) -> None:
        rows = seeded_db.get_symbols(country="CA")
        assert len(rows) == 1
        assert rows[0]["ticker"] == "SHOP"

    def test_min_price(self, seeded_db: UniverseDB) -> None:
        rows = seeded_db.get_symbols(min_price=200.0)
        tickers = {r["ticker"] for r in rows}
        assert "MSFT" in tickers and "SPY" in tickers
        assert "AAPL" not in tickers  # 190 < 200

    def test_search_ticker(self, seeded_db: UniverseDB) -> None:
        rows = seeded_db.get_symbols(search="GOO")
        assert len(rows) == 1
        assert rows[0]["ticker"] == "GOOG"

    def test_limit_and_offset(self, seeded_db: UniverseDB) -> None:
        page1 = seeded_db.get_symbols(limit=3, offset=0)
        page2 = seeded_db.get_symbols(limit=3, offset=3)
        assert len(page1) == 3
        assert len(page2) >= 1
        tickers_1 = {r["ticker"] for r in page1}
        tickers_2 = {r["ticker"] for r in page2}
        assert tickers_1.isdisjoint(tickers_2)

    def test_inactive_excluded_by_default(self, seeded_db: UniverseDB) -> None:
        seeded_db.mark_inactive(["SHOP"])
        rows = seeded_db.get_symbols()
        tickers = {r["ticker"] for r in rows}
        assert "SHOP" not in tickers

    def test_inactive_included_when_flag_false(self, seeded_db: UniverseDB) -> None:
        seeded_db.mark_inactive(["SHOP"])
        rows = seeded_db.get_symbols(is_active=False)
        tickers = {r["ticker"] for r in rows}
        assert "SHOP" in tickers


# ---------------------------------------------------------------------------
# search_symbols
# ---------------------------------------------------------------------------

class TestSearchSymbols:
    def test_partial_ticker_match(self, seeded_db: UniverseDB) -> None:
        results = seeded_db.search_symbols("MS")
        tickers = {r["ticker"] for r in results}
        assert "MSFT" in tickers

    def test_partial_name_match(self, seeded_db: UniverseDB) -> None:
        results = seeded_db.search_symbols("Exxon")
        assert any(r["ticker"] == "XOM" for r in results)

    def test_limit_respected(self, seeded_db: UniverseDB) -> None:
        results = seeded_db.search_symbols("", limit=2)
        assert len(results) == 2


# ---------------------------------------------------------------------------
# get_sectors / get_exchanges / get_stats
# ---------------------------------------------------------------------------

class TestAggregates:
    def test_get_sectors(self, seeded_db: UniverseDB) -> None:
        sectors = seeded_db.get_sectors()
        names = {s["sector"] for s in sectors}
        assert "Technology" in names and "Energy" in names
        tech = next(s for s in sectors if s["sector"] == "Technology")
        assert tech["count"] == 4  # AAPL, MSFT, GOOG, SHOP

    def test_get_exchanges(self, seeded_db: UniverseDB) -> None:
        exchanges = seeded_db.get_exchanges()
        names = {e["exchange"] for e in exchanges}
        assert "NASDAQ" in names and "NYSE" in names

    def test_get_stats_keys(self, seeded_db: UniverseDB) -> None:
        stats = seeded_db.get_stats()
        assert stats["total_symbols"] == len(SAMPLE_SYMBOLS)
        assert stats["active_symbols"] == len(SAMPLE_SYMBOLS)
        assert "Technology" in stats["by_sector"]
        assert "NASDAQ" in stats["by_exchange"]
        assert "STOCK" in stats["by_asset_type"]
        assert stats["last_updated"] is not None


# ---------------------------------------------------------------------------
# Watchlists
# ---------------------------------------------------------------------------

class TestWatchlists:
    def test_add_and_get(self, seeded_db: UniverseDB) -> None:
        count = seeded_db.add_to_watchlist("picks", ["AAPL", "MSFT"])
        assert count == 2
        wl = seeded_db.get_watchlist("picks")
        tickers = {r["ticker"] for r in wl}
        assert tickers == {"AAPL", "MSFT"}

    def test_add_duplicate_ignored(self, seeded_db: UniverseDB) -> None:
        seeded_db.add_to_watchlist("picks", ["AAPL"])
        count = seeded_db.add_to_watchlist("picks", ["AAPL"])
        assert count == 0

    def test_add_nonexistent_ticker_skipped(self, seeded_db: UniverseDB) -> None:
        count = seeded_db.add_to_watchlist("picks", ["DOESNOTEXIST"])
        assert count == 0

    def test_remove_from_watchlist(self, seeded_db: UniverseDB) -> None:
        seeded_db.add_to_watchlist("picks", ["AAPL", "MSFT", "GOOG"])
        removed = seeded_db.remove_from_watchlist("picks", ["MSFT"])
        assert removed == 1
        remaining = {r["ticker"] for r in seeded_db.get_watchlist("picks")}
        assert remaining == {"AAPL", "GOOG"}

    def test_get_active_watchlist(self, seeded_db: UniverseDB) -> None:
        seeded_db.add_to_watchlist("a", ["AAPL", "MSFT"])
        seeded_db.add_to_watchlist("b", ["GOOG", "AAPL"])  # AAPL in both
        active = seeded_db.get_active_watchlist()
        assert active == ["AAPL", "GOOG", "MSFT"]  # sorted, deduplicated

    def test_get_watchlist_names(self, seeded_db: UniverseDB) -> None:
        seeded_db.add_to_watchlist("alpha", ["AAPL"])
        seeded_db.add_to_watchlist("beta", ["MSFT", "GOOG"])
        names = seeded_db.get_watchlist_names()
        name_map = {n["list_name"]: n["count"] for n in names}
        assert name_map == {"alpha": 1, "beta": 2}

    def test_set_watchlist_enabled(self, seeded_db: UniverseDB) -> None:
        seeded_db.add_to_watchlist("picks", ["AAPL", "MSFT"])
        seeded_db.set_watchlist_enabled("picks", False)
        active = seeded_db.get_active_watchlist()
        assert "AAPL" not in active and "MSFT" not in active

    def test_delete_watchlist(self, seeded_db: UniverseDB) -> None:
        seeded_db.add_to_watchlist("gone", ["AAPL"])
        seeded_db.delete_watchlist("gone")
        assert seeded_db.get_watchlist("gone") == []

    def test_empty_add(self, seeded_db: UniverseDB) -> None:
        assert seeded_db.add_to_watchlist("x", []) == 0

    def test_empty_remove(self, seeded_db: UniverseDB) -> None:
        assert seeded_db.remove_from_watchlist("x", []) == 0


# ---------------------------------------------------------------------------
# Scan filters
# ---------------------------------------------------------------------------

class TestScanFilters:
    def test_set_and_get(self, universe_db: UniverseDB) -> None:
        universe_db.set_scan_filter("min_price", 10.0)
        filters = universe_db.get_scan_filters()
        assert filters["min_price"] == 10.0

    def test_disabled_filter_excluded(self, universe_db: UniverseDB) -> None:
        universe_db.set_scan_filter("min_price", 5.0, enabled=False)
        filters = universe_db.get_scan_filters()
        assert "min_price" not in filters


# ---------------------------------------------------------------------------
# bulk_update / mark_inactive
# ---------------------------------------------------------------------------

class TestBulkOps:
    def test_bulk_update_price(self, seeded_db: UniverseDB) -> None:
        seeded_db.bulk_update([{"ticker": "AAPL", "last_price": 200.0}])
        rows = seeded_db.get_symbols(search="AAPL")
        assert rows[0]["last_price"] == 200.0

    def test_bulk_update_multiple_fields(self, seeded_db: UniverseDB) -> None:
        seeded_db.bulk_update([{
            "ticker": "MSFT",
            "last_price": 450.0,
            "avg_volume": 30_000_000,
        }])
        row = seeded_db.get_symbols(search="MSFT")[0]
        assert row["last_price"] == 450.0
        assert row["avg_volume"] == 30_000_000

    def test_bulk_update_empty(self, seeded_db: UniverseDB) -> None:
        # Should not raise
        seeded_db.bulk_update([])

    def test_mark_inactive(self, seeded_db: UniverseDB) -> None:
        seeded_db.mark_inactive(["XOM", "JPM"])
        active = seeded_db.get_symbols()
        active_tickers = {r["ticker"] for r in active}
        assert "XOM" not in active_tickers
        assert "JPM" not in active_tickers

    def test_mark_inactive_empty(self, seeded_db: UniverseDB) -> None:
        seeded_db.mark_inactive([])  # should not raise


# ---------------------------------------------------------------------------
# Filtered universe / tiers
# ---------------------------------------------------------------------------

class TestTiers:
    def test_get_filtered_universe(self, seeded_db: UniverseDB) -> None:
        # Default filters: price>=5, volume>=100k, market_cap>=300M
        tickers = seeded_db.get_filtered_universe()
        # SHOP has market_cap=90B but last_price=85 — passes all defaults
        assert "AAPL" in tickers
        assert "SHOP" in tickers

    def test_filtered_universe_respects_custom_filter(self, seeded_db: UniverseDB) -> None:
        seeded_db.set_scan_filter("min_price", 150.0)
        tickers = seeded_db.get_filtered_universe()
        # SHOP last_price=85 and XOM last_price=105 should be excluded
        assert "SHOP" not in tickers
        assert "XOM" not in tickers
        assert "MSFT" in tickers  # 410

    def test_tier1_equals_active_watchlist(self, seeded_db: UniverseDB) -> None:
        seeded_db.add_to_watchlist("t1", ["AAPL", "MSFT"])
        assert seeded_db.get_tier1_symbols() == seeded_db.get_active_watchlist()

    def test_tier2_symbols_by_sector(self, seeded_db: UniverseDB) -> None:
        tickers = seeded_db.get_tier2_symbols("Financials")
        assert tickers == ["JPM"]

    def test_tier3_equals_filtered_universe(self, seeded_db: UniverseDB) -> None:
        assert seeded_db.get_tier3_symbols() == seeded_db.get_filtered_universe()


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestThreadSafety:
    def test_concurrent_inserts(self, tmp_path: Path) -> None:
        db = UniverseDB(tmp_path / "universe.db")
        errors: list[Exception] = []

        def insert_batch(prefix: str) -> None:
            try:
                syms = [
                    _sym(f"{prefix}{i}", exchange="NASDAQ", asset_type="STOCK")
                    for i in range(50)
                ]
                db.add_symbols(syms)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=insert_batch, args=(f"T{n}_",)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        all_rows = db.get_symbols(is_active=True)
        assert len(all_rows) == 200  # 4 threads x 50 symbols
