"""Tests for the dashboard performance-analytics API endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
from config.settings import Settings
from journal.trade_logger import TradeLogger
from signals.signal_types import ExitEvent, ExitReason, Signal, TradeOrder


@pytest.fixture
def client() -> TestClient:
    return TestClient(dash.app, raise_server_exceptions=False)


def _seed_journal(data_dir) -> None:
    """Write a couple of completed trades to trades.csv."""
    journal = TradeLogger(str(data_dir))
    for sym, entry, exit_px, reason in [
        ("AAPL", 100.0, 115.0, ExitReason.TARGET_HIT),
        ("MSFT", 200.0, 190.0, ExitReason.STOP_HIT),
    ]:
        sig = Signal(symbol=sym, strategy="momentum", entry_price=entry,
                     stop_price=entry * 0.95, target_price=entry * 1.15,
                     signal_strength=0.8)
        order = TradeOrder(signal=sig, quantity=10, currency="USD",
                           ai_decision="APPROVE", ai_reasoning="ok")
        journal.log_entry(order, entry, commission=0.05)
        journal.log_exit(
            sym,
            ExitEvent(symbol=sym, exit_price=exit_px, exit_reason=reason),
            exit_commission=0.05,
        )


def _use_settings(monkeypatch, data_dir) -> None:
    settings = Settings(DASHBOARD_AUTH_ENABLED=False, DATA_DIR=data_dir)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)


def test_analytics_summary_endpoint(client, monkeypatch, tmp_path) -> None:
    _seed_journal(tmp_path)
    _use_settings(monkeypatch, tmp_path)
    resp = client.get("/api/analytics/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_trades"] == 2
    assert body["wins"] == 1 and body["losses"] == 1
    assert "sharpe_ratio" in body and "max_drawdown_pct" in body


def test_analytics_by_strategy_endpoint(client, monkeypatch, tmp_path) -> None:
    _seed_journal(tmp_path)
    _use_settings(monkeypatch, tmp_path)
    resp = client.get("/api/analytics/by-strategy")
    assert resp.status_code == 200
    rows = resp.json()["by_strategy"]
    assert any(r["strategy"] == "momentum" and r["trades"] == 2 for r in rows)


def test_analytics_by_symbol_endpoint(client, monkeypatch, tmp_path) -> None:
    _seed_journal(tmp_path)
    _use_settings(monkeypatch, tmp_path)
    resp = client.get("/api/analytics/by-symbol")
    assert resp.status_code == 200
    symbols = {r["symbol"] for r in resp.json()["by_symbol"]}
    assert {"AAPL", "MSFT"} <= symbols


def test_analytics_equity_curve_endpoint(client, monkeypatch, tmp_path) -> None:
    _seed_journal(tmp_path)
    _use_settings(monkeypatch, tmp_path)
    resp = client.get("/api/analytics/equity-curve")
    assert resp.status_code == 200
    curve = resp.json()["equity_curve"]
    assert len(curve) == 2


def test_analytics_report_endpoint(client, monkeypatch, tmp_path) -> None:
    _seed_journal(tmp_path)
    _use_settings(monkeypatch, tmp_path)
    resp = client.get("/api/analytics/report")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"summary", "by_strategy", "by_symbol", "equity_curve", "recent_trades"}


def test_analytics_requires_auth_when_enabled(client, monkeypatch, tmp_path) -> None:
    settings = Settings(DASHBOARD_AUTH_ENABLED=True, DASHBOARD_PASSWORD="secret", DATA_DIR=tmp_path)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    resp = client.get("/api/analytics/summary")
    assert resp.status_code == 401


def test_analytics_summary_empty_journal(client, monkeypatch, tmp_path) -> None:
    _use_settings(monkeypatch, tmp_path)  # no journal written
    resp = client.get("/api/analytics/summary")
    assert resp.status_code == 200
    assert resp.json()["total_trades"] == 0
