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


# ── Gap 1: dynamic ETF recognition ──────────────────────────────────────────


def test_is_etf_static_only():
    from config.etf_universe import is_etf_static

    assert is_etf_static("SPY")
    assert is_etf_static("xlk")
    assert not is_etf_static("VTI")   # not in the static 15
    assert not is_etf_static("AAPL")


def _point_settings_at(monkeypatch, tmp_data_dir):
    """Make config.etf_universe's internal get_settings() see *tmp_data_dir*."""
    import config.settings as cs
    from config.settings import Settings

    s = Settings(DATA_DIR=tmp_data_dir)
    monkeypatch.setattr(cs, "get_settings", lambda: s)
    return s


def test_is_etf_via_db_asset_type(monkeypatch, tmp_data_dir):
    """A symbol not in the static list is recognised via the DB asset_type."""
    from config.etf_universe import clear_etf_cache, is_etf
    from data_store.universe import UniverseDB

    db = UniverseDB(tmp_data_dir / "universe.db")
    db.add_symbols(
        [
            {"ticker": "VTI", "exchange": "NYSE", "asset_type": "etf"},
            {"ticker": "SCHD", "exchange": "NYSE", "asset_type": "etf"},
            {"ticker": "ACME", "exchange": "NYSE", "asset_type": "stock"},
        ]
    )
    _point_settings_at(monkeypatch, tmp_data_dir)
    clear_etf_cache()

    assert is_etf("VTI") is True      # dynamic recognition of a user-added ETF
    assert is_etf("SCHD") is True
    assert is_etf("ACME") is False    # DB says stock
    assert is_etf("SPY") is True      # static still works with a DB present


def test_is_etf_yfinance_last_resort(monkeypatch, tmp_data_dir):
    """With no DB row, a live quoteType lookup decides ETF-ness (last resort)."""
    from config.etf_universe import clear_etf_cache, is_etf
    from data import etf_metadata

    # tmp_data_dir has no universe.db → DB layer returns None, static misses.
    _point_settings_at(monkeypatch, tmp_data_dir)

    class _T:
        def __init__(self, qt):
            self._qt = qt

        @property
        def info(self):
            return {"quoteType": self._qt}

    monkeypatch.setattr(
        etf_metadata,
        "_ticker_factory",
        lambda sym: _T("ETF" if sym == "ARKK" else "EQUITY"),
    )
    etf_metadata.clear_cache()
    clear_etf_cache()

    assert is_etf("ARKK") is True
    assert is_etf("TSLA") is False


def test_is_etf_failopen_to_static(monkeypatch, tmp_data_dir):
    """yfinance failure must not break recognition of statically-known ETFs."""
    from config.etf_universe import clear_etf_cache, is_etf
    from data import etf_metadata

    _point_settings_at(monkeypatch, tmp_data_dir)

    def _boom(sym):
        raise RuntimeError("network down")

    monkeypatch.setattr(etf_metadata, "_ticker_factory", _boom)
    etf_metadata.clear_cache()
    clear_etf_cache()

    assert is_etf("SPY") is True          # static list still recognised
    assert is_etf("UNKNOWNXYZ") is False  # unknown + no network → not an ETF


def test_universe_db_asset_type_roundtrip(tmp_data_dir):
    from data_store.universe import UniverseDB

    db = UniverseDB(tmp_data_dir / "universe.db")
    db.add_symbols([{"ticker": "VOO", "exchange": "NYSE", "asset_type": "stock"}])
    assert db.get_asset_type("VOO") == "stock"
    assert db.get_asset_type("voo") == "stock"   # case-insensitive
    assert db.get_asset_type("NOPE") is None

    assert db.set_asset_type("VOO", "etf") is True
    assert db.get_asset_type("VOO") == "etf"
    assert db.set_asset_type("MISSING", "etf") is False


# ── Gap 3: leveraged / inverse ETF sizing ───────────────────────────────────


def test_leveraged_etf_sized_smaller_than_regular_etf():
    """A 3x fund (TQQQ) gets a far smaller position than a plain ETF (SPY)."""
    settings = Settings()
    rm = RiskManager(settings)
    reg = rm.build_order(_signal("SPY", 20.0, 19.0), "APPROVE", "", 0.0)
    lev = rm.build_order(_signal("TQQQ", 20.0, 19.0), "APPROVE", "", 0.0)
    assert reg is not None and lev is not None
    assert lev.quantity < reg.quantity


def test_inverse_and_leveraged_inverse_ordering():
    """Risk-budget ordering: regular > inverse > 3x-inverse."""
    settings = Settings()
    rm = RiskManager(settings)
    reg = rm.build_order(_signal("SPY", 20.0, 19.0), "APPROVE", "", 0.0)
    inv = rm.build_order(_signal("SH", 20.0, 19.0), "APPROVE", "", 0.0)     # 1x inverse
    linv = rm.build_order(_signal("SQQQ", 20.0, 19.0), "APPROVE", "", 0.0)  # 3x inverse
    assert reg and inv and linv
    assert reg.quantity > inv.quantity > linv.quantity


def test_etf_risk_params_mapping():
    settings = Settings()
    rm = RiskManager(settings)
    # Regular ETF → standard ETF params.
    mod, cap, cat = rm._etf_risk_params("SPY")
    assert cat == "regular"
    assert mod == settings.ETF_RISK_MODIFIER
    assert cap == settings.ETF_NOTIONAL_CAP_PCT
    # 3x leveraged → shrunk params.
    mod, cap, cat = rm._etf_risk_params("TQQQ")
    assert cat == "leveraged_3x"
    assert mod == settings.LEVERAGED_3X_RISK_MODIFIER
    assert cap == settings.LEVERAGED_3X_NOTIONAL_CAP_PCT
    # 3x inverse.
    mod, cap, cat = rm._etf_risk_params("SQQQ")
    assert cat == "leveraged_inverse"
    assert mod == settings.LEVERAGED_INVERSE_RISK_MODIFIER


def test_leveraged_etf_logs_warning():
    from structlog.testing import capture_logs

    settings = Settings()
    rm = RiskManager(settings)
    with capture_logs() as logs:
        rm.build_order(_signal("TQQQ", 20.0, 19.0), "APPROVE", "", 0.0)
    events = [e for e in logs if e.get("event") == "build_order.leveraged_etf_sized"]
    assert events, "expected a leveraged-ETF sizing warning"
    assert events[0]["log_level"] == "warning"
    assert events[0]["leverage"] == "leveraged_3x"


def test_regular_etf_does_not_log_leverage_warning():
    from structlog.testing import capture_logs

    settings = Settings()
    rm = RiskManager(settings)
    with capture_logs() as logs:
        rm.build_order(_signal("SPY", 20.0, 19.0), "APPROVE", "", 0.0)
    assert not [e for e in logs if e.get("event") == "build_order.leveraged_etf_sized"]
