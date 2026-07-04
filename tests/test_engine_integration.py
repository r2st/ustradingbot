"""Engine-level integration tests for the new order-type routing, pending-entry
reconciliation, risk alerts, and mode-switch restart wiring."""

from __future__ import annotations

import structlog

import pytest

import engine as engine_mod
from config.settings import Settings
from signals.signal_types import Grade, Signal, TradeOrder

log = structlog.get_logger("test")


@pytest.fixture
def eng(monkeypatch, tmp_data_dir):
    """A TradingEngine bound to a temp data dir with the paper broker."""
    settings = Settings(DATA_DIR=tmp_data_dir, BROKER="paper", AI_VETO_ENABLED=False)
    monkeypatch.setattr(engine_mod, "get_settings", lambda: settings)
    return engine_mod.TradingEngine()


def _order(symbol="AAPL", qty=10):
    sig = Signal(symbol=symbol, strategy="momentum", entry_price=100.0,
                 stop_price=95.0, target_price=130.0, signal_strength=0.85,
                 grade=Grade.A, direction="long")
    return sig, TradeOrder(signal=sig, quantity=qty, currency="USD",
                           ai_decision="APPROVE", ai_reasoning="ok")


# --------------------------------------------------------------------------- #
# order-type routing
# --------------------------------------------------------------------------- #


def test_default_immediate_bracket_registers(eng) -> None:
    sig, order = _order()
    pending, accepted = eng._place_entry(sig, order, "USD", log)
    assert pending is False and accepted is True
    assert eng.risk_manager.get_open_positions().get("AAPL") is not None
    assert eng.broker.get_positions() == {"AAPL": 10}


def test_scale_in_rests_until_filled(eng, monkeypatch) -> None:
    eng.settings = Settings(
        DATA_DIR=eng.settings.DATA_DIR, BROKER="paper",
        ENABLE_SCALE_IN=True, SCALE_IN_TRANCHES=3, AI_VETO_ENABLED=False,
    )
    sig, order = _order(qty=30)
    pending, accepted = eng._place_entry(sig, order, "USD", log)
    assert pending is True and accepted is True
    # Not registered yet — resting.
    assert eng.risk_manager.get_open_positions() == {}
    assert len(eng._pending_orders) == 3


async def test_reconcile_pending_registers(eng, monkeypatch) -> None:
    eng.settings = Settings(
        DATA_DIR=eng.settings.DATA_DIR, BROKER="paper",
        ENABLE_SCALE_IN=True, SCALE_IN_TRANCHES=3, AI_VETO_ENABLED=False,
    )
    sig, order = _order(qty=30)
    eng._place_entry(sig, order, "USD", log)

    # Price dips to fill every tranche.
    monkeypatch.setattr("execution.broker.fetch_current_price", lambda s: 97.0)
    n = await eng._reconcile_pending_entries()
    assert n == 3
    pos = eng.risk_manager.get_open_positions()["AAPL"]
    assert pos["quantity"] == 30
    assert eng._pending_orders == {}


async def test_reconcile_expiry_drops_order(eng, monkeypatch) -> None:
    from datetime import datetime, timedelta

    eng.settings = Settings(DATA_DIR=eng.settings.DATA_DIR, BROKER="paper",
                            AI_VETO_ENABLED=False)
    sig, order = _order()
    res = eng.broker.place_limit_order("AAPL", 10, 99.0, 95.0, 130.0, expiry_hours=4.0)
    eng._pending_orders[res.order_id] = order
    # Backdate so it expires.
    eng.broker._pending["AAPL"][0].placed_at = (
        datetime.now() - timedelta(hours=5)
    ).isoformat()
    monkeypatch.setattr("execution.broker.fetch_current_price", lambda s: 98.0)
    await eng._reconcile_pending_entries()
    assert eng.risk_manager.get_open_positions() == {}
    assert eng._pending_orders == {}


def test_moc_routing_when_past_cutoff(eng, monkeypatch) -> None:
    eng.settings = Settings(DATA_DIR=eng.settings.DATA_DIR, BROKER="paper",
                            ENABLE_MOC_ENTRIES=True, AI_VETO_ENABLED=False)
    monkeypatch.setattr(eng, "_is_past_order_cutoff", lambda: True)
    sig, order = _order()
    pending, accepted = eng._place_entry(sig, order, "USD", log)
    assert pending is True and accepted is True
    assert len(eng._pending_orders) == 1
    # A MOC pending order was recorded on the broker.
    assert eng.broker.get_pending_orders()["AAPL"][0]["kind"] == "moc"


# --------------------------------------------------------------------------- #
# risk alerts
# --------------------------------------------------------------------------- #


async def test_check_risk_alerts_fires_drawdown(eng, monkeypatch) -> None:
    fired = {"dd": False, "daily": False}

    async def fake_dd(pct):
        fired["dd"] = True
        return True

    async def fake_daily(pnl, cap, day=None):
        fired["daily"] = True
        return False

    monkeypatch.setattr(eng.notifier, "check_drawdown", fake_dd)
    monkeypatch.setattr(eng.notifier, "check_daily_loss", fake_daily)

    # Seed a losing journal so an equity curve (and drawdown) exists.
    from journal.trade_logger import TradeLogger
    from signals.signal_types import ExitEvent, ExitReason
    journal = TradeLogger(str(eng.settings.DATA_DIR))
    sig, order = _order()
    journal.log_entry(order, 100.0)
    journal.log_exit("AAPL", ExitEvent(symbol="AAPL", exit_price=80.0,
                                       exit_reason=ExitReason.STOP_HIT))

    await eng._check_risk_alerts()
    assert fired["dd"] is True
    assert fired["daily"] is True


# --------------------------------------------------------------------------- #
# mode-switch restart
# --------------------------------------------------------------------------- #


def test_restart_requested_consumes_sentinel(eng) -> None:
    from dashboard.mode_control import request_restart

    assert eng._restart_requested() is False
    request_restart(eng.settings.DATA_DIR, "live")
    assert eng._restart_requested() is True
    # Consumed — a second check is False.
    assert eng._restart_requested() is False
