"""Tests for the paper-trading dashboard section, banner, and API endpoints."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
from config.settings import Settings
from journal.trade_logger import TradeLogger
from signals.signal_types import ExitEvent, ExitReason, Signal, TradeOrder


@pytest.fixture
def client() -> TestClient:
    return TestClient(dash.app, raise_server_exceptions=False)


def _seed(data_dir) -> None:
    """Write one open position and one closed trade to *data_dir*."""
    (data_dir / "open_positions.json").write_text(
        json.dumps(
            {
                "AAPL": {
                    "symbol": "AAPL", "strategy": "momentum", "entry_price": 195.5,
                    "stop_price": 190.0, "target_price": 208.0, "quantity": 10,
                    "currency": "USD", "risk_amount": 55.0, "grade": "A",
                    "entry_time": "2026-07-04T10:00:00",
                }
            }
        )
    )
    journal = TradeLogger(str(data_dir))
    sig = Signal(symbol="MSFT", strategy="swing", entry_price=200.0,
                 stop_price=190.0, target_price=230.0, signal_strength=0.7)
    order = TradeOrder(signal=sig, quantity=5, currency="USD",
                       ai_decision="APPROVE", ai_reasoning="ok")
    journal.log_entry(order, 200.0, commission=0.05)
    journal.log_exit(
        "MSFT",
        ExitEvent(symbol="MSFT", exit_price=230.0, exit_reason=ExitReason.TARGET_HIT),
        exit_commission=0.05,
    )


def _use(monkeypatch, data_dir, **kw) -> None:
    settings = Settings(DASHBOARD_AUTH_ENABLED=False, DATA_DIR=data_dir, **kw)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)


# ------------------------------------------------------------ banner / render


def test_dashboard_shows_paper_banner_by_default(client, monkeypatch, tmp_path) -> None:
    _use(monkeypatch, tmp_path)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Paper Trading" in resp.text
    assert "Simulated money" in resp.text
    assert "How to Use Paper Trading" in resp.text


def test_dashboard_shows_live_banner_when_live(client, monkeypatch, tmp_path) -> None:
    _use(monkeypatch, tmp_path, BROKER="ibkr", IBKR_PORT=7496)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Live Trading" in resp.text
    assert "Real money is at risk" in resp.text


def test_dashboard_renders_positions_and_history(client, monkeypatch, tmp_path) -> None:
    _seed(tmp_path)
    _use(monkeypatch, tmp_path)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "AAPL" in resp.text          # open position
    assert "MSFT" in resp.text          # closed trade
    assert "TARGET_HIT" in resp.text


def test_dashboard_empty_state_renders(client, monkeypatch, tmp_path) -> None:
    _use(monkeypatch, tmp_path)  # no data seeded
    resp = client.get("/")
    assert resp.status_code == 200
    assert "No open positions yet" in resp.text


# ------------------------------------------------------------ /api/mode + health


def test_api_mode_is_public_and_paper(client, monkeypatch, tmp_path) -> None:
    _use(monkeypatch, tmp_path)
    body = client.get("/api/mode").json()
    assert body["trading_mode"] == "PAPER"
    assert body["is_paper"] is True
    assert body["is_live"] is False


def test_api_mode_live(client, monkeypatch, tmp_path) -> None:
    _use(monkeypatch, tmp_path, BROKER="ibkr", IBKR_PORT=7496)
    body = client.get("/api/mode").json()
    assert body["trading_mode"] == "LIVE"
    assert body["is_live"] is True


def test_health_reports_mode(client, monkeypatch, tmp_path) -> None:
    _use(monkeypatch, tmp_path)
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["trading_mode"] == "PAPER"


# ------------------------------------------------------------ /api/paper/*


def test_paper_summary(client, monkeypatch, tmp_path) -> None:
    _seed(tmp_path)
    _use(monkeypatch, tmp_path)
    body = client.get("/api/paper/summary").json()
    assert body["mode"] == "PAPER"
    assert body["open_count"] == 1
    assert body["total_trades"] == 1
    # MSFT target hit: (230-200)*5 = 150 gross, minus 0.10 commission = 149.90.
    assert body["realized_pnl"] == pytest.approx(149.90)
    assert body["account_equity"] == pytest.approx(12_000.0 + 149.90)
    # balances present per configured currency.
    currencies = {b["currency"] for b in body["balances"]}
    assert "USD" in currencies


def test_paper_positions(client, monkeypatch, tmp_path) -> None:
    _seed(tmp_path)
    _use(monkeypatch, tmp_path)
    body = client.get("/api/paper/positions").json()
    assert body["open_count"] == 1
    pos = body["positions"][0]
    assert pos["symbol"] == "AAPL"
    assert pos["cost_basis"] == pytest.approx(195.5 * 10)


def test_paper_trades(client, monkeypatch, tmp_path) -> None:
    _seed(tmp_path)
    _use(monkeypatch, tmp_path)
    trades = client.get("/api/paper/trades").json()["trades"]
    assert len(trades) == 1
    assert trades[0]["symbol"] == "MSFT"
    assert trades[0]["exit_reason"] == "TARGET_HIT"
    assert trades[0]["pnl_net"] == pytest.approx(149.90)


def test_paper_summary_requires_auth_when_enabled(client, monkeypatch, tmp_path) -> None:
    settings = Settings(DASHBOARD_AUTH_ENABLED=True, DASHBOARD_PASSWORD="secret",
                        DATA_DIR=tmp_path)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    assert client.get("/api/paper/summary").status_code == 401
    # /api/mode stays public even with auth on.
    assert client.get("/api/mode").status_code == 200


def test_paper_summary_empty_journal(client, monkeypatch, tmp_path) -> None:
    _use(monkeypatch, tmp_path)
    body = client.get("/api/paper/summary").json()
    assert body["open_count"] == 0
    assert body["total_trades"] == 0
    assert body["realized_pnl"] == 0.0
    assert body["account_equity"] == pytest.approx(body["starting_capital"])
