"""Tests for ETF support (Feature 3): universe tagging, sizing, ATR floor."""

from __future__ import annotations

from config.etf_universe import (
    ALL_ETFS,
    BROAD_MARKET_ETFS,
    SECTOR_ETFS,
    asset_type,
    etf_for_sector,
    is_etf,
    sector_etf_symbols,
    sector_for_etf,
)
from config.settings import Settings
from config.universe import asset_type as universe_asset_type
from config.universe import get_sector
from risk.manager import RiskManager
from signals.signal_types import Grade, Signal


# ── etf_universe helpers ────────────────────────────────────────────────────


def test_is_etf_membership():
    assert is_etf("SPY")
    assert is_etf("xlk")  # case-insensitive
    assert not is_etf("AAPL")
    assert not is_etf("")


def test_asset_type_classification():
    assert asset_type("QQQ") == "etf"
    assert asset_type("NVDA") == "stock"


def test_sector_etf_round_trip():
    assert sector_for_etf("XLK") == "Technology"
    assert etf_for_sector("Technology") == "XLK"
    # Broad-market ETFs track no single sector.
    assert sector_for_etf("SPY") is None
    # Non-ETF has no sector ETF mapping.
    assert sector_for_etf("AAPL") is None


def test_all_etfs_include_broad_and_sector():
    for sym in BROAD_MARKET_ETFS:
        assert sym in ALL_ETFS
    for sym in SECTOR_ETFS.values():
        assert sym in ALL_ETFS
    assert len(sector_etf_symbols()) == 11


# ── config.universe integration ─────────────────────────────────────────────


def test_universe_asset_type_delegates():
    assert universe_asset_type("XLE") == "etf"
    assert universe_asset_type("MSFT") == "stock"


def test_get_sector_resolves_etf_to_tracked_sector():
    assert get_sector("XLK") == "Technology"
    assert get_sector("XLE") == "Energy"
    # A stock still resolves normally.
    assert get_sector("AAPL") == "Technology"


# ── ETF sizing branch in RiskManager.build_order ────────────────────────────


def _signal(symbol: str, entry: float, stop: float) -> Signal:
    target = entry + (entry - stop) * 2.0
    return Signal(
        symbol=symbol,
        strategy="momentum",
        direction="long",
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        signal_strength=0.82,
        grade=Grade.A,
    )


def test_etf_gets_larger_size_than_stock():
    settings = Settings()
    rm = RiskManager(settings)
    # Same price levels; only the asset type differs.
    etf_order = rm.build_order(_signal("SPY", 20.0, 19.0), "APPROVE", "", 0.0)
    stock_order = rm.build_order(_signal("AAPL", 20.0, 19.0), "APPROVE", "", 0.0)
    assert etf_order is not None and stock_order is not None
    assert etf_order.quantity > stock_order.quantity


def test_etf_notional_cap_is_larger():
    settings = Settings()
    rm = RiskManager(settings)
    # High share price forces the notional cap to bind for both; the ETF cap
    # (15%) should still allow more shares than the stock cap (10%).
    etf_order = rm.build_order(_signal("QQQ", 300.0, 285.0), "APPROVE", "", 0.0)
    stock_order = rm.build_order(_signal("NVDA", 300.0, 285.0), "APPROVE", "", 0.0)
    assert etf_order is not None and stock_order is not None
    assert etf_order.quantity > stock_order.quantity


# ── ETF watchlist seed ──────────────────────────────────────────────────────


def test_default_watchlist_seeds_etf_list(tmp_data_dir):
    from config.watchlist import WatchlistStore

    store = WatchlistStore(tmp_data_dir)
    lists = store.as_dict()
    assert "ETFs" in lists
    assert "SPY" in lists["ETFs"]["symbols"]
    assert "XLK" in lists["ETFs"]["symbols"]
