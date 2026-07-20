"""Tests for the per-position stop-trade (manual close) feature."""

from __future__ import annotations

import json

import pytest

from config.settings import Settings
from execution.broker import PaperBroker
from execution.stop_trade import stop_open_position
from journal.trade_logger import TradeLogger
from risk.manager import RiskManager
from signals.signal_types import Grade, Signal, TradeOrder


def _open_paper_position(settings: Settings, symbol="AAPL", entry=100.0,
                         qty=5, side="long") -> PaperBroker:
    broker = PaperBroker(settings)
    result = broker.place_bracket_order(
        symbol=symbol,
        quantity=qty,
        entry_price=entry,
        stop_price=entry * (1.05 if side == "short" else 0.95),
        target_price=entry * (0.90 if side == "short" else 1.10),
        side=side,
    )
    assert result.accepted
    return broker


def _register_risk_position(settings: Settings, symbol="AAPL", entry=100.0,
                            qty=5, side="long") -> RiskManager:
    rm = RiskManager(settings)
    order = TradeOrder(
        signal=Signal(
            symbol=symbol,
            strategy="momentum",
            direction=side,
            entry_price=entry,
            stop_price=entry * (1.05 if side == "short" else 0.95),
            target_price=entry * (0.90 if side == "short" else 1.10),
            signal_strength=0.8,
            grade=Grade.A,
        ),
        quantity=qty,
    )
    rm.register_position(order, entry)
    return rm


# ------------------------------------------------------------- core close


def test_stop_open_position_closes_and_journals(settings, monkeypatch):
    monkeypatch.setattr(
        "execution.broker.fetch_current_price", lambda s: 110.0
    )
    broker = _open_paper_position(settings)
    rm = _register_risk_position(settings)
    logger = TradeLogger(str(settings.DATA_DIR))
    logger.log_entry(
        TradeOrder(
            signal=Signal(symbol="AAPL", strategy="momentum",
                          entry_price=100.0, stop_price=95.0,
                          target_price=110.0, grade=Grade.A),
            quantity=5,
        ),
        100.0,
        0.0,
    )

    result = stop_open_position(
        "aapl", settings, broker=broker, risk_manager=rm, trade_logger=logger
    )
    assert result.ok, result.message
    assert result.symbol == "AAPL"
    assert result.exit_price == pytest.approx(110.0)
    assert "Stopped AAPL" in result.message

    # Broker no longer tracks the position.
    assert "AAPL" not in broker.get_positions()
    # Risk state no longer tracks it either (persisted).
    assert "AAPL" not in rm.get_open_positions()
    positions_file = settings.DATA_DIR / "open_positions.json"
    assert json.loads(positions_file.read_text()) == {}
    # The journal recorded a MANUAL exit.
    trades = logger.get_recent_trades(5)
    assert len(trades) == 1
    assert trades.iloc[0]["exit_reason"] == "MANUAL"


def test_stop_open_position_short_pnl_sign(settings, monkeypatch):
    # Price dropped 100 -> 90: a short close is a gain.
    monkeypatch.setattr(
        "execution.broker.fetch_current_price", lambda s: 90.0
    )
    broker = _open_paper_position(settings, side="short")
    rm = _register_risk_position(settings, side="short")

    result = stop_open_position(
        "AAPL", settings, broker=broker, risk_manager=rm,
        trade_logger=TradeLogger(str(settings.DATA_DIR)),
    )
    assert result.ok
    assert result.pnl_gross is not None and result.pnl_gross > 0


def test_stop_open_position_unknown_symbol(settings):
    broker = PaperBroker(settings)
    result = stop_open_position(
        "ZZZZ", settings, broker=broker,
        risk_manager=RiskManager(settings),
        trade_logger=TradeLogger(str(settings.DATA_DIR)),
    )
    assert result.ok is False
    assert "No open position" in result.message


# ------------------------------------------- cross-process broker state


def test_paper_broker_adopts_foreign_close(settings, monkeypatch):
    """A close performed by another process (dashboard) must be adopted by
    the engine's in-memory broker instead of being clobbered/resurrected."""
    monkeypatch.setattr(
        "execution.broker.fetch_current_price", lambda s: 101.0
    )
    engine_broker = _open_paper_position(settings)
    assert engine_broker.get_positions() == {"AAPL": 5}

    # Simulate the dashboard process: a second broker over the same state.
    from signals.signal_types import ExitReason

    dashboard_broker = PaperBroker(settings)
    event = dashboard_broker.force_close("AAPL", ExitReason.MANUAL)
    assert event is not None

    # The engine-side broker re-reads the shared file before acting.
    assert engine_broker.get_positions() == {}
    assert engine_broker.poll_exits() == []
    state = json.loads((settings.DATA_DIR / "paper_broker.json").read_text())
    assert state["positions"] == {}


# ----------------------------------------------------------------- router


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


def test_stop_endpoint_requires_admin_password(client):
    r = client.post("/api/positions/stop",
                    json={"symbol": "AAPL", "admin_password": "wrong"})
    assert r.status_code == 403


def test_stop_endpoint_requires_symbol(client):
    # `symbol` is a required, pattern-validated body field now, so omitting it
    # is a request-validation error (422), caught before the broker is touched.
    r = client.post("/api/positions/stop",
                    json={"admin_password": "adminpw"})
    assert r.status_code == 422


def test_stop_endpoint_closes_position(client, monkeypatch):
    from execution.stop_trade import StopTradeResult

    calls = {}

    def fake_stop(symbol, settings, **kwargs):
        calls["symbol"] = symbol
        return StopTradeResult(True, f"Stopped {symbol}.", symbol, 101.0, 5, 5.0)

    monkeypatch.setattr("execution.stop_trade.stop_open_position", fake_stop)
    r = client.post("/api/positions/stop",
                    json={"symbol": "aapl", "admin_password": "adminpw"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["symbol"] == "AAPL"
    assert calls["symbol"] == "AAPL"
