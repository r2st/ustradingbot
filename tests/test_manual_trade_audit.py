"""
Tests for the manual-trade audit trail (audit item B-2).

The manual-trade money path must leave a reconstructable record of who placed
what, for which symbol/quantity/side, from which IP — on both a successful
placement and a rejected admin password.
"""

from __future__ import annotations

import structlog
from fastapi.testclient import TestClient


def _client(monkeypatch, tmp_path, **env):
    data_dir = tmp_path / "data_store"
    data_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "adminpw")
    monkeypatch.setenv("DASHBOARD_ADMIN_PASSWORD", "trade-pw")
    monkeypatch.setenv("TOTAL_CAPITAL", "10000")
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))

    from config.settings import get_settings

    get_settings.cache_clear()
    import dashboard.app as dash

    return TestClient(dash.app, raise_server_exceptions=False)


_TRADE = {
    "symbol": "aapl",
    "side": "buy",
    "quantity": 10,
    "entry_price": 100.0,
    "stop_price": 95.0,
    "target_price": 110.0,
}


def test_bad_password_is_audited(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, DASHBOARD_AUTH_ENABLED=False)
    body = {**_TRADE, "admin_password": "wrong"}
    with structlog.testing.capture_logs() as logs:
        resp = client.post("/api/manual-trade", json=body)
    assert resp.status_code == 403
    rec = next(e for e in logs if e.get("event") == "manual_trade.bad_password")
    assert rec["log_level"] == "warning"
    assert rec["symbol"] == "AAPL"
    assert rec["quantity"] == 10
    assert rec["side"] == "buy"
    assert "ip" in rec and "user" in rec


def test_successful_trade_is_audited(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, DASHBOARD_AUTH_ENABLED=False)

    from execution.manual_trade import ManualTradeResult

    def _fake_place(body, settings):
        return ManualTradeResult(
            ok=True, message="filled", symbol="AAPL", quantity=10,
            fill_price=100.5, order_id="OID42",
        )

    monkeypatch.setattr("execution.manual_trade.place_manual_trade", _fake_place)

    body = {**_TRADE, "admin_password": "trade-pw"}
    with structlog.testing.capture_logs() as logs:
        resp = client.post("/api/manual-trade", json=body)
    assert resp.status_code == 200
    rec = next(e for e in logs if e.get("event") == "manual_trade.placed")
    assert rec["log_level"] == "info"
    assert rec["symbol"] == "AAPL"
    assert rec["quantity"] == 10
    assert rec["side"] == "buy"
    assert rec["order_id"] == "OID42"
    assert rec["fill_price"] == 100.5
    assert rec["ok"] is True
    assert "ip" in rec and "user" in rec


def test_broker_rejection_is_audited_as_warning(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, DASHBOARD_AUTH_ENABLED=False)

    from execution.manual_trade import ManualTradeResult

    def _fake_place(body, settings):
        return ManualTradeResult(ok=False, message="broker rejected", symbol="AAPL")

    monkeypatch.setattr("execution.manual_trade.place_manual_trade", _fake_place)

    body = {**_TRADE, "admin_password": "trade-pw"}
    with structlog.testing.capture_logs() as logs:
        resp = client.post("/api/manual-trade", json=body)
    assert resp.status_code == 200
    rec = next(e for e in logs if e.get("event") == "manual_trade.rejected")
    assert rec["log_level"] == "warning"
    assert rec["ok"] is False
