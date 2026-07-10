"""Tests for the index-membership, Scan Pool, and promotion features of UniverseDB."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from config.settings import EASTERN
from data_store.universe import UniverseDB


@pytest.fixture()
def db(tmp_path: Path) -> UniverseDB:
    return UniverseDB(tmp_path / "universe.db")


def _stock(ticker: str, **kw) -> dict:
    d = {"ticker": ticker, "exchange": "", "asset_type": "stock"}
    d.update(kw)
    return d


# ---------------------------------------------------------------------------
# Index membership
# ---------------------------------------------------------------------------


class TestIndexMembership:
    def test_set_and_get(self, db: UniverseDB) -> None:
        db.set_index_membership("SP500", ["AAPL", "MSFT", "XOM"])
        assert db.get_index_symbols("SP500") == ["AAPL", "MSFT", "XOM"]

    def test_set_replaces_previous(self, db: UniverseDB) -> None:
        db.set_index_membership("SP500", ["AAPL", "MSFT"])
        db.set_index_membership("SP500", ["AAPL", "GOOGL"])  # MSFT dropped
        assert db.get_index_symbols("SP500") == ["AAPL", "GOOGL"]

    def test_uppercases(self, db: UniverseDB) -> None:
        db.set_index_membership("SP500", ["aapl"])
        assert db.get_index_symbols("SP500") == ["AAPL"]

    def test_index_universe_is_union(self, db: UniverseDB) -> None:
        db.set_index_membership("SP500", ["AAPL", "XOM"])
        db.set_index_membership("NASDAQ100", ["AAPL", "NVDA"])
        assert db.get_index_universe() == ["AAPL", "NVDA", "XOM"]

    def test_is_in_index(self, db: UniverseDB) -> None:
        db.set_index_membership("NASDAQ100", ["NVDA"])
        assert db.is_in_index("NVDA", "NASDAQ100") is True
        assert db.is_in_index("nvda", "NASDAQ100") is True
        assert db.is_in_index("XOM", "NASDAQ100") is False

    def test_membership_survives_without_symbols_row(self, db: UniverseDB) -> None:
        # Membership is intentionally FK-free — a constituent need not exist in
        # the symbols table yet.
        db.set_index_membership("SP500", ["NEWCO"])
        assert "NEWCO" in db.get_index_symbols("SP500")


# ---------------------------------------------------------------------------
# Scan Pool (Tier 2) ranking
# ---------------------------------------------------------------------------


class TestScanPool:
    def test_empty_when_no_membership(self, db: UniverseDB) -> None:
        assert db.get_scan_pool("SP500") == []

    def test_unranked_returns_ticker_order(self, db: UniverseDB) -> None:
        db.set_index_membership("SP500", ["MSFT", "AAPL", "XOM"])
        # No symbols metadata -> all liquidity 0 -> stable ticker order.
        assert db.get_scan_pool("SP500") == ["AAPL", "MSFT", "XOM"]

    def test_ranks_by_liquidity(self, db: UniverseDB) -> None:
        db.set_index_membership("SP500", ["AAPL", "MSFT", "XOM"])
        db.add_symbols([
            _stock("AAPL", avg_volume=1e8, market_cap=3e12),   # liq = 3e20
            _stock("MSFT", avg_volume=5e7, market_cap=2.5e12), # liq = 1.25e20
            _stock("XOM", avg_volume=1e7, market_cap=4e11),    # liq = 4e18
        ])
        assert db.get_scan_pool("SP500") == ["AAPL", "MSFT", "XOM"]

    def test_limit_truncates(self, db: UniverseDB) -> None:
        db.set_index_membership("SP500", ["AAPL", "MSFT", "XOM"])
        db.add_symbols([
            _stock("AAPL", avg_volume=1e8, market_cap=3e12),
            _stock("MSFT", avg_volume=5e7, market_cap=2.5e12),
            _stock("XOM", avg_volume=1e7, market_cap=4e11),
        ])
        assert db.get_scan_pool("SP500", limit=2) == ["AAPL", "MSFT"]

    def test_excludes_inactive(self, db: UniverseDB) -> None:
        db.set_index_membership("SP500", ["AAPL", "MSFT"])
        db.add_symbols([
            _stock("AAPL", avg_volume=1e8, market_cap=3e12),
            _stock("MSFT", avg_volume=5e7, market_cap=2.5e12),
        ])
        db.mark_inactive(["MSFT"])
        assert db.get_scan_pool("SP500") == ["AAPL"]


# ---------------------------------------------------------------------------
# ensure_symbols
# ---------------------------------------------------------------------------


class TestEnsureSymbols:
    def test_inserts_missing(self, db: UniverseDB) -> None:
        assert db.ensure_symbols([_stock("AAPL")]) == 1
        assert db.get_symbols(search="AAPL")

    def test_preserves_enrichment(self, db: UniverseDB) -> None:
        db.add_symbols([_stock("AAPL", sector="Technology", market_cap=3e12)])
        # ensure_symbols must NOT clobber the enriched row.
        inserted = db.ensure_symbols([_stock("AAPL", sector="", market_cap=None)])
        assert inserted == 0
        row = db.get_symbols(search="AAPL")[0]
        assert row["sector"] == "Technology"
        assert row["market_cap"] == 3e12


# ---------------------------------------------------------------------------
# Promotions (Tier 2/3 -> Tier 1)
# ---------------------------------------------------------------------------


class TestPromotions:
    def test_promote_and_get(self, db: UniverseDB) -> None:
        db.promote_symbol("TSLA", source_tier="tier2", reason="momentum A")
        assert db.get_promoted_symbols() == ["TSLA"]

    def test_promote_uppercases(self, db: UniverseDB) -> None:
        db.promote_symbol("tsla")
        assert db.get_promoted_symbols() == ["TSLA"]

    def test_repromote_refreshes(self, db: UniverseDB) -> None:
        db.promote_symbol("TSLA", ttl_hours=1)
        db.promote_symbol("TSLA", ttl_hours=100)  # replace, not duplicate
        assert db.get_promoted_symbols() == ["TSLA"]
        assert len(db.get_promotions()) == 1

    def test_expired_not_returned(self, db: UniverseDB) -> None:
        # Write a promotion that already expired by back-dating expires_at.
        db.promote_symbol("TSLA", ttl_hours=1)
        past = (
            (__import__("datetime").datetime.now(tz=EASTERN) - timedelta(hours=2))
            .isoformat(timespec="seconds")
        )
        with db._lock:  # noqa: SLF001 — test reaches into the store deliberately
            db._conn.execute(
                "UPDATE promotions SET expires_at = ? WHERE ticker = ?",
                (past, "TSLA"),
            )
            db._conn.commit()
        assert db.get_promoted_symbols() == []

    def test_never_expires_when_ttl_nonpositive(self, db: UniverseDB) -> None:
        db.promote_symbol("TSLA", ttl_hours=0)
        assert db.get_promotions()[0]["expires_at"] is None
        assert db.expire_promotions() == 0
        assert db.get_promoted_symbols() == ["TSLA"]

    def test_expire_removes_stale(self, db: UniverseDB) -> None:
        db.promote_symbol("TSLA", ttl_hours=1)
        past = (
            (__import__("datetime").datetime.now(tz=EASTERN) - timedelta(hours=2))
            .isoformat(timespec="seconds")
        )
        with db._lock:  # noqa: SLF001
            db._conn.execute(
                "UPDATE promotions SET expires_at = ? WHERE ticker = ?",
                (past, "TSLA"),
            )
            db._conn.commit()
        assert db.expire_promotions() == 1
        assert db.get_promotions() == []

    def test_demote(self, db: UniverseDB) -> None:
        db.promote_symbol("TSLA")
        db.demote_symbol("tsla")
        assert db.get_promoted_symbols() == []
