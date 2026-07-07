"""Tests for the watchlist monitor endpoint (monitoring F8)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
import dashboard.quotes as quotes
from config.settings import Settings
from journal.activity_log import write_last_scan
from journal.btst_logger import RejectedSignalLogger
from signals.signal_types import Grade, Signal


@pytest.fixture
def env(monkeypatch, tmp_path):
    data_dir = tmp_path / "data_store"
    data_dir.mkdir()
    settings = Settings(DASHBOARD_AUTH_ENABLED=False, DATA_DIR=data_dir)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    # A single enabled watchlist so scan_symbols_for is deterministic.
    (data_dir / "watchlists.json").write_text(json.dumps({
        "default": {"symbols": ["NVDA", "AAPL", "MSFT", "TSLA"], "enabled": True},
    }))
    # Fresh watchlist store cache (module-level singleton keyed by path).
    import config.watchlist as wl

    monkeypatch.setattr(wl, "_stores", {}, raising=False)
    quotes.clear_cache()
    monkeypatch.setattr(
        quotes, "_fetch_price",
        lambda s: {"NVDA": 150.0, "AAPL": 200.0, "MSFT": 400.0}.get(s),
    )
    monkeypatch.setattr(quotes, "_fetch_ohlcv", lambda s: None)
    client = TestClient(dash.app, raise_server_exceptions=False)
    yield client, data_dir
    quotes.clear_cache()


def _sig(symbol: str) -> Signal:
    return Signal(symbol=symbol, strategy="momentum", entry_price=148.9,
                  stop_price=144.0, target_price=158.0,
                  signal_strength=0.83, grade=Grade.A)


def test_statuses(env):
    client, data_dir = env
    # NVDA produced a signal in the latest scan.
    write_last_scan(data_dir, "c1", [_sig("NVDA")])
    # AAPL is held.
    (data_dir / "open_positions.json").write_text(json.dumps({
        "AAPL": {"symbol": "AAPL", "entry_price": 190.0, "quantity": 5},
    }))
    # MSFT was rejected at cash_check.
    RejectedSignalLogger(str(data_dir)).log_rejection(
        _sig("MSFT"), "cash_check", "need 4000, have 100")

    d = client.get("/api/watchlist/monitor").json()
    by_sym = {r["symbol"]: r for r in d["symbols"]}

    assert by_sym["NVDA"]["status"] == "signal"
    assert by_sym["NVDA"]["signal"]["grade"] == "A"
    assert by_sym["NVDA"]["price"] == 150.0

    assert by_sym["AAPL"]["status"] == "held"  # held wins over anything else
    assert by_sym["MSFT"]["status"] == "rejected"
    assert by_sym["MSFT"]["last_rejection"]["gate"] == "cash_check"

    # TSLA: no quote configured → stale, never an error.
    assert by_sym["TSLA"]["price"] is None
    assert by_sym["TSLA"]["stale"] is True
    assert d["last_scan_at"] is not None


def test_signal_and_rejection_shown_together(env):
    client, data_dir = env
    write_last_scan(data_dir, "c1", [_sig("NVDA")])
    RejectedSignalLogger(str(data_dir)).log_rejection(
        _sig("NVDA"), "cash_check", "no cash")
    row = {r["symbol"]: r for r in
           client.get("/api/watchlist/monitor").json()["symbols"]}["NVDA"]
    # Scored grade A but rejected at cash_check → both visible.
    assert row["status"] == "signal"
    assert row["signal"]["grade"] == "A"
    assert row["last_rejection"]["gate"] == "cash_check"


def test_near_entry_from_freshness_rejection(env):
    client, data_dir = env
    RejectedSignalLogger(str(data_dir)).log_rejection(
        _sig("NVDA"), "freshness_check", "price_drifted: 1.4% > 1.0%")
    row = {r["symbol"]: r for r in
           client.get("/api/watchlist/monitor").json()["symbols"]}["NVDA"]
    assert row["status"] == "near_entry"


def test_trade_selection_exclusion(env):
    client, data_dir = env
    (data_dir / "trade_selection.json").write_text(json.dumps({
        "enabled": True, "symbols": ["NVDA"], "strategies": [], "min_grade": "B",
    }))
    d = client.get("/api/watchlist/monitor").json()
    by_sym = {r["symbol"]: r for r in d["symbols"]}
    assert by_sym["AAPL"]["status"] == "excluded"
    assert by_sym["NVDA"]["status"] == "idle"
