"""Tests for the index-based tier helpers in config.universe (DB + fallback)."""

from __future__ import annotations

from pathlib import Path

import pytest

import config.settings as settings_mod
from config import index_membership as im
from config.settings import Settings
from config.universe import (
    expire_promotions,
    get_index_universe_symbols,
    get_promoted_tier1_symbols,
    get_scan_pool_symbols,
    promote_to_tier1,
)
from data_store.universe import UniverseDB


# ---------------------------------------------------------------------------
# Fallback path (no universe.db on disk)
# ---------------------------------------------------------------------------


class TestFallbackNoDB:
    @pytest.fixture(autouse=True)
    def _point_at_empty_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # DATA_DIR with no universe.db -> every helper takes the static path.
        s = Settings(DATA_DIR=tmp_path)
        monkeypatch.setattr(settings_mod, "get_settings", lambda: s)

    def test_scan_pool_uses_static_priority(self) -> None:
        pool = get_scan_pool_symbols(limit=5)
        # Static fallback leads with the liquidity-priority order.
        assert pool == im.SP500_LIQUIDITY_PRIORITY[:5]

    def test_scan_pool_unlimited_covers_sp500(self) -> None:
        pool = get_scan_pool_symbols()
        assert set(pool) == set(im.sp500_symbols())

    def test_index_universe_is_static_union(self) -> None:
        assert get_index_universe_symbols() == im.index_universe()

    def test_promotion_is_noop_without_db(self) -> None:
        assert promote_to_tier1("TSLA") is False
        assert get_promoted_tier1_symbols() == []
        assert expire_promotions() == 0


# ---------------------------------------------------------------------------
# DB-backed path
# ---------------------------------------------------------------------------


class TestWithDB:
    @pytest.fixture()
    def data_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        # Create + seed a universe.db, then point get_settings at it.
        db = UniverseDB(tmp_path / "universe.db")
        db.set_index_membership("SP500", ["AAPL", "MSFT", "XOM"])
        db.set_index_membership("NASDAQ100", ["AAPL", "NVDA"])
        db.add_symbols([
            {"ticker": "AAPL", "exchange": "", "asset_type": "stock",
             "avg_volume": 1e8, "market_cap": 3e12},
            {"ticker": "MSFT", "exchange": "", "asset_type": "stock",
             "avg_volume": 5e7, "market_cap": 2.5e12},
            {"ticker": "XOM", "exchange": "", "asset_type": "stock",
             "avg_volume": 1e7, "market_cap": 4e11},
        ])
        s = Settings(DATA_DIR=tmp_path)
        monkeypatch.setattr(settings_mod, "get_settings", lambda: s)
        return tmp_path

    def test_scan_pool_ranked_from_db(self, data_dir: Path) -> None:
        assert get_scan_pool_symbols() == ["AAPL", "MSFT", "XOM"]

    def test_scan_pool_limit(self, data_dir: Path) -> None:
        assert get_scan_pool_symbols(limit=2) == ["AAPL", "MSFT"]

    def test_index_universe_from_db(self, data_dir: Path) -> None:
        assert get_index_universe_symbols() == ["AAPL", "MSFT", "NVDA", "XOM"]

    def test_promotion_round_trip(self, data_dir: Path) -> None:
        assert promote_to_tier1("TSLA", source_tier="tier2", reason="momentum A") is True
        assert get_promoted_tier1_symbols() == ["TSLA"]

    def test_expire_promotions_returns_count(self, data_dir: Path) -> None:
        promote_to_tier1("TSLA", ttl_hours=0)  # never expires
        assert expire_promotions() == 0
        assert get_promoted_tier1_symbols() == ["TSLA"]
