"""Tests for user-controlled trade selection (feature 1)."""

from __future__ import annotations

import json

import pytest

from config.trade_selection import (
    TradeSelection,
    TradeSelectionError,
    load_trade_selection,
    save_trade_selection,
)


# ----------------------------------------------------------------- store


def test_default_is_disabled(tmp_data_dir):
    sel = load_trade_selection(tmp_data_dir)
    assert sel.enabled is False
    assert sel.symbols == [] and sel.strategies == []
    assert sel.min_grade == "B"


def test_save_load_roundtrip(tmp_data_dir):
    saved = save_trade_selection(tmp_data_dir, {
        "enabled": True,
        "symbols": ["nvda", "AAPL", "nvda"],       # normalised + de-duped
        "strategies": ["Momentum", "vcp_breakout"],
        "min_grade": "a",
    })
    assert saved.symbols == ["NVDA", "AAPL"]
    assert saved.strategies == ["momentum", "vcp_breakout"]
    assert saved.min_grade == "A"

    loaded = load_trade_selection(tmp_data_dir)
    assert loaded.enabled is True
    assert loaded.symbols == ["NVDA", "AAPL"]
    assert loaded.min_grade == "A"


@pytest.mark.parametrize("bad", [
    {"strategies": ["day_trading"]},
    {"min_grade": "F"},
    {"min_grade": "Z"},
    {"symbols": ["not a symbol!!"]},
    {"symbols": "NVDA"},
])
def test_save_rejects_invalid(tmp_data_dir, bad):
    with pytest.raises(TradeSelectionError):
        save_trade_selection(tmp_data_dir, bad)


def test_corrupt_file_degrades_to_disabled(tmp_data_dir):
    (tmp_data_dir / "trade_selection.json").write_text("{not json", encoding="utf-8")
    sel = load_trade_selection(tmp_data_dir)
    assert sel.enabled is False


def test_unknown_strategy_on_load_fails_closed(tmp_data_dir):
    """A version-skewed file must NOT silently disable the selection.

    Unknown strategy ids are kept: they whitelist nothing, so the engine
    trades less, never more (the old degrade-to-disabled behaviour meant a
    single unknown id made the engine ignore the whole selection and trade
    everything — the "selected A, traded B" inconsistency).
    """
    (tmp_data_dir / "trade_selection.json").write_text(
        json.dumps({"enabled": True, "strategies": ["nonsense"],
                    "min_grade": "A"}),
        encoding="utf-8",
    )
    sel = load_trade_selection(tmp_data_dir)
    assert sel.enabled is True
    assert sel.strategies == ["nonsense"]
    assert sel.min_grade == "A"
    # The unknown id restricts: no scanner strategy matches it.
    assert sel.allowed_strategies() == ["nonsense"]


def test_bad_min_grade_on_load_falls_back(tmp_data_dir):
    (tmp_data_dir / "trade_selection.json").write_text(
        json.dumps({"enabled": True, "min_grade": "Z"}), encoding="utf-8"
    )
    sel = load_trade_selection(tmp_data_dir)
    assert sel.enabled is True
    assert sel.min_grade == "B"


def test_short_strategies_are_selectable(tmp_data_dir):
    saved = save_trade_selection(tmp_data_dir, {
        "enabled": True,
        "strategies": ["short_relative_weakness", "momentum"],
    })
    assert saved.strategies == ["short_relative_weakness", "momentum"]
    loaded = load_trade_selection(tmp_data_dir)
    assert loaded.strategies == ["short_relative_weakness", "momentum"]


# ---------------------------------------------------------- engine hooks


def test_filter_symbols_disabled_passthrough():
    sel = TradeSelection(enabled=False, symbols=["NVDA"])
    assert sel.filter_symbols(["AAPL", "MSFT"]) == ["AAPL", "MSFT"]


def test_filter_symbols_empty_selection_passthrough():
    sel = TradeSelection(enabled=True, symbols=[])
    assert sel.filter_symbols(["AAPL", "MSFT"]) == ["AAPL", "MSFT"]


def test_filter_symbols_restricts_and_keeps_explicit_picks():
    sel = TradeSelection(enabled=True, symbols=["NVDA", "TSLA"])
    # TSLA is not in the watchlist scan set but was explicitly picked.
    assert sel.filter_symbols(["AAPL", "NVDA", "MSFT"]) == ["NVDA", "TSLA"]


def test_allowed_strategies_and_min_grade():
    off = TradeSelection(enabled=False, strategies=["swing"], min_grade="A")
    assert off.allowed_strategies() is None
    assert off.effective_min_grade("B") == "B"

    on = TradeSelection(enabled=True, strategies=["swing"], min_grade="A")
    assert on.allowed_strategies() == ["swing"]
    assert on.effective_min_grade("B") == "A"

    on_all = TradeSelection(enabled=True, strategies=[])
    assert on_all.allowed_strategies() is None


# ----------------------------------------------------- entry pipeline gate


def _fake_signal(symbol="AAPL", strategy="momentum", grade="B"):
    from signals.signal_types import Grade, Signal

    return Signal(
        symbol=symbol,
        strategy=strategy,
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        signal_strength=0.7,
        grade=Grade(grade),
    )


def test_allows_signal_disabled_allows_everything():
    sel = TradeSelection(enabled=False, strategies=["swing"], min_grade="A")
    ok, reason = sel.allows_signal(_fake_signal(strategy="momentum", grade="C"))
    assert ok is True and reason == ""


def test_allows_signal_blocks_unselected_strategy():
    sel = TradeSelection(enabled=True, strategies=["momentum"], min_grade="B")
    ok, reason = sel.allows_signal(_fake_signal(strategy="swing"))
    assert ok is False and "swing" in reason


def test_allows_signal_blocks_below_min_grade():
    sel = TradeSelection(enabled=True, min_grade="A")
    ok, reason = sel.allows_signal(_fake_signal(grade="B"))
    assert ok is False and "below the selected minimum" in reason
    ok, _ = sel.allows_signal(_fake_signal(grade="A"))
    assert ok is True


def test_allows_signal_blocks_unselected_symbol():
    sel = TradeSelection(enabled=True, symbols=["NVDA"])
    ok, reason = sel.allows_signal(_fake_signal(symbol="AAPL"))
    assert ok is False and "AAPL" in reason


def test_allows_signal_short_strategy_whitelisted():
    sel = TradeSelection(
        enabled=True, strategies=["short_relative_weakness"], min_grade="B"
    )
    ok, _ = sel.allows_signal(
        _fake_signal(strategy="short_relative_weakness", grade="A")
    )
    assert ok is True
    ok, _ = sel.allows_signal(_fake_signal(strategy="momentum"))
    assert ok is False


# -------------------------------------------------------- screener filter


def test_scan_symbol_honours_strategy_whitelist(monkeypatch, bullish_df):
    import signals.screener as scr

    attempted = []

    def _fake_score(symbol, strategy, df):
        attempted.append(strategy)
        return None

    monkeypatch.setattr(scr, "fetch_ohlcv", lambda *a, **k: bullish_df)
    monkeypatch.setattr(scr, "score_symbol", _fake_score)
    monkeypatch.setattr(
        scr, "_DEDICATED_DETECTORS",
        {k: (lambda sym, df: None) for k in scr._DEDICATED_DETECTORS},
    )

    scr._scan_symbol("AAPL", "B", allowed_strategies=["momentum", "swing"])
    # Only the generic-score strategies in the whitelist were attempted.
    assert attempted == ["momentum", "swing"]


# ---------------------------------------------------------------- router


@pytest.fixture
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data_store"
    data_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "False")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "adminpw")
    from config.settings import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    import dashboard.app as dash
    c = TestClient(dash.app, raise_server_exceptions=False)
    yield c
    get_settings.cache_clear()


def test_router_get_defaults(client):
    r = client.get("/api/trade-selection")
    assert r.status_code == 200
    body = r.json()
    assert body["selection"]["enabled"] is False
    assert "vcp_breakout" in body["strategies"]
    assert body["grades"] == ["A", "B", "C"]


def test_router_post_requires_admin_password(client):
    r = client.post("/api/trade-selection", json={
        "enabled": True, "symbols": ["NVDA"], "admin_password": "wrong",
    })
    assert r.status_code == 403


def test_router_post_saves_and_reads_back(client):
    r = client.post("/api/trade-selection", json={
        "enabled": True,
        "symbols": ["nvda"],
        "strategies": ["momentum"],
        "min_grade": "A",
        "admin_password": "adminpw",
    })
    assert r.status_code == 200 and r.json()["ok"] is True

    r = client.get("/api/trade-selection")
    sel = r.json()["selection"]
    assert sel == {
        "enabled": True, "symbols": ["NVDA"], "strategies": ["momentum"],
        "min_grade": "A", "updated_at": sel["updated_at"],
    }
    assert sel["updated_at"]


def test_router_post_invalid_returns_message(client):
    r = client.post("/api/trade-selection", json={
        "enabled": True, "strategies": ["hodl"], "admin_password": "adminpw",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "hodl" in r.json()["message"]
