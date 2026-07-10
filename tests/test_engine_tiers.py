"""Engine-level tests for the index-based tier loop and auto-promotion wiring."""

from __future__ import annotations

import structlog

import pytest

import config.settings as settings_mod
import engine as engine_mod
from config.settings import Settings
from data_store.universe import UniverseDB
from signals.signal_types import Grade, Signal

log = structlog.get_logger("test")


@pytest.fixture
def eng(monkeypatch, tmp_data_dir):
    """A TradingEngine on a temp data dir with a seeded universe.db."""
    # Seed a small index universe so the tier helpers have real data.
    db = UniverseDB(tmp_data_dir / "universe.db")
    db.set_index_membership("SP500", ["AAPL", "MSFT", "XOM", "NVDA"])
    db.set_index_membership("NASDAQ100", ["AAPL", "NVDA"])
    db.add_symbols([
        {"ticker": "AAPL", "exchange": "", "asset_type": "stock",
         "avg_volume": 1e8, "market_cap": 3e12},
        {"ticker": "MSFT", "exchange": "", "asset_type": "stock",
         "avg_volume": 5e7, "market_cap": 2.5e12},
        {"ticker": "XOM", "exchange": "", "asset_type": "stock",
         "avg_volume": 1e7, "market_cap": 4e11},
        {"ticker": "NVDA", "exchange": "", "asset_type": "stock",
         "avg_volume": 2e8, "market_cap": 3.2e12},
    ])
    settings = Settings(DATA_DIR=tmp_data_dir, BROKER="paper", AI_VETO_ENABLED=False)
    monkeypatch.setattr(engine_mod, "get_settings", lambda: settings)
    # config.universe tier helpers resolve settings via config.settings.get_settings
    # (same singleton as the engine in production); point it at the temp dir too.
    monkeypatch.setattr(settings_mod, "get_settings", lambda: settings)
    return engine_mod.TradingEngine()


def _sig(symbol: str) -> Signal:
    return Signal(symbol=symbol, strategy="momentum", entry_price=100.0,
                  stop_price=95.0, target_price=130.0, signal_strength=0.85,
                  grade=Grade.A, direction="long")


class _Selection:
    """Minimal stand-in for a TradeSelection: passes everything through."""

    enabled = False

    def filter_symbols(self, symbols):
        return list(symbols)

    def effective_min_grade(self, default):
        return default

    def allowed_strategies(self):
        return None


# ---------------------------------------------------------------------------
# Promotion wiring
# ---------------------------------------------------------------------------


def test_promote_signals_records_tier1_promotion(eng) -> None:
    eng._promote_signals([_sig("NVDA"), _sig("MSFT")], "tier2")
    from config.universe import get_promoted_tier1_symbols

    assert set(get_promoted_tier1_symbols()) == {"NVDA", "MSFT"}


def test_promote_signals_empty_is_noop(eng) -> None:
    eng._promote_signals([], "tier2")
    from config.universe import get_promoted_tier1_symbols

    assert get_promoted_tier1_symbols() == []


def test_promoted_symbol_dedup(eng) -> None:
    # Same symbol twice in one batch -> single promotion row.
    eng._promote_signals([_sig("NVDA"), _sig("NVDA")], "tier3")
    from data_store.universe import get_universe_db

    db = get_universe_db(eng.settings.DATA_DIR)
    assert db.get_promoted_symbols() == ["NVDA"]


# ---------------------------------------------------------------------------
# Tier 2 (daily Scan Pool)
# ---------------------------------------------------------------------------


def test_tier2_scans_pool_and_promotes(eng, monkeypatch) -> None:
    captured = {}

    def fake_full_scan(symbols, **kw):
        captured["symbols"] = symbols
        return [_sig("NVDA")]

    monkeypatch.setattr(engine_mod, "run_full_scan", fake_full_scan)
    eng._tier1_symbol_set = {"AAPL"}  # already covered every cycle -> excluded

    signals = eng._run_tier2_scan(_Selection())

    assert "AAPL" not in captured["symbols"]     # excluded (in Tier 1)
    assert "NVDA" in captured["symbols"]          # from the S&P 500 scan pool
    assert len(signals) == 1
    from config.universe import get_promoted_tier1_symbols
    assert "NVDA" in get_promoted_tier1_symbols()  # signal auto-promoted


def test_tier2_runs_once_per_day(eng, monkeypatch) -> None:
    monkeypatch.setattr(engine_mod, "run_full_scan", lambda symbols, **kw: [])
    eng._tier1_symbol_set = set()
    assert eng._run_tier2_scan(_Selection()) == []      # first run: executes
    # Second call the same day is throttled to an empty result without scanning.
    calls = {"n": 0}

    def counting_scan(symbols, **kw):
        calls["n"] += 1
        return []

    monkeypatch.setattr(engine_mod, "run_full_scan", counting_scan)
    assert eng._run_tier2_scan(_Selection()) == []
    assert calls["n"] == 0


# ---------------------------------------------------------------------------
# Tier 3 (weekly Universe sweep)
# ---------------------------------------------------------------------------


def test_tier3_prescreens_index_universe_and_promotes(eng, monkeypatch) -> None:
    prescreened = {}

    def fake_prescreen(symbols, **kw):
        prescreened["universe"] = symbols
        return ["XOM"]  # one qualifier

    def fake_full_scan(symbols, **kw):
        return [_sig("XOM")]

    monkeypatch.setattr(engine_mod, "run_prescreen", fake_prescreen)
    monkeypatch.setattr(engine_mod, "run_full_scan", fake_full_scan)
    eng._tier1_symbol_set = set()

    signals = eng._run_tier3_scan(_Selection())

    # Prescreen ran over the S&P 500 ∪ NASDAQ-100 membership.
    assert set(prescreened["universe"]) == {"AAPL", "MSFT", "XOM", "NVDA"}
    assert len(signals) == 1
    from config.universe import get_promoted_tier1_symbols
    assert "XOM" in get_promoted_tier1_symbols()


def test_tier3_runs_once_per_week(eng, monkeypatch) -> None:
    monkeypatch.setattr(engine_mod, "run_prescreen", lambda symbols, **kw: [])
    eng._tier1_symbol_set = set()
    eng._run_tier3_scan(_Selection())  # sets the week marker

    calls = {"n": 0}

    def counting_prescreen(symbols, **kw):
        calls["n"] += 1
        return []

    monkeypatch.setattr(engine_mod, "run_prescreen", counting_prescreen)
    assert eng._run_tier3_scan(_Selection()) == []
    assert calls["n"] == 0
