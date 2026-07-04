"""Tests for the execution layer: paper broker, factory, and exit manager."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.settings import Settings
from execution.broker import IBKRBroker, PaperBroker, make_broker
from execution.exit_manager import ExitManager
from journal.trade_logger import TradeLogger
from risk.manager import RiskManager
from signals.signal_types import ExitReason, Signal, TradeOrder


def _order(symbol: str = "AAPL", qty: int = 10) -> TradeOrder:
    sig = Signal(
        symbol=symbol,
        strategy="momentum",
        entry_price=100.0,
        stop_price=95.0,
        target_price=115.0,
        signal_strength=0.82,
    )
    return TradeOrder(signal=sig, quantity=qty, currency="USD",
                      ai_decision="APPROVE", ai_reasoning="ok")


# ---------------------------------------------------------------- factory

def test_make_broker_defaults_to_paper(settings: Settings) -> None:
    assert isinstance(make_broker(settings), PaperBroker)


def test_make_broker_ibkr_selected(tmp_data_dir: Path) -> None:
    s = Settings(DATA_DIR=tmp_data_dir, BROKER="ibkr")
    assert isinstance(make_broker(s), IBKRBroker)


# ---------------------------------------------------------------- paper broker

def test_paper_broker_place_and_track(settings: Settings) -> None:
    broker = PaperBroker(settings)
    assert broker.connect() is True
    res = broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 115.0)
    # Entry fills above the requested price by PAPER_SLIPPAGE_BPS (5 bp default)
    # and is charged a per-share commission.
    assert res.accepted
    assert res.fill_price == pytest.approx(100.0 * 1.0005)
    assert res.commission == pytest.approx(10 * 0.005)
    assert broker.get_positions() == {"AAPL": 10}


def test_paper_broker_rejects_duplicate(settings: Settings) -> None:
    broker = PaperBroker(settings)
    broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 115.0)
    res = broker.place_bracket_order("AAPL", 5, 100.0, 95.0, 115.0)
    assert not res.accepted and res.reason == "already_open"


def test_paper_broker_rejects_zero_qty(settings: Settings) -> None:
    broker = PaperBroker(settings)
    res = broker.place_bracket_order("AAPL", 0, 100.0, 95.0, 115.0)
    assert not res.accepted


def test_paper_broker_stop_only_moves_up(settings: Settings) -> None:
    broker = PaperBroker(settings)
    broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 115.0)
    assert broker.modify_stop("AAPL", 98.0) is True
    assert broker.modify_stop("AAPL", 96.0) is False  # lower rejected
    assert broker.get_position_detail("AAPL")["stop_price"] == 98.0


def test_paper_broker_force_close(settings: Settings, monkeypatch) -> None:
    broker = PaperBroker(settings)
    broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 115.0)
    monkeypatch.setattr("execution.broker.fetch_current_price", lambda s: 110.0)
    ev = broker.force_close("AAPL", ExitReason.TIME_EXIT_FLAT)
    assert ev is not None
    assert ev.exit_price == 110.0
    # Entry filled at 100.05 (5 bp slippage), so gross P&L is off that basis.
    assert ev.pnl_gross == pytest.approx((110.0 - 100.05) * 10)
    assert ev.fill_details["commission"] == pytest.approx(10 * 0.005)
    assert broker.get_positions() == {}


def test_paper_broker_persists_across_instances(settings: Settings) -> None:
    b1 = PaperBroker(settings)
    b1.place_bracket_order("AAPL", 7, 100.0, 95.0, 115.0)
    b2 = PaperBroker(settings)  # reload from disk
    assert b2.get_positions() == {"AAPL": 7}


def test_paper_broker_poll_exit_stop_hit(settings: Settings, monkeypatch) -> None:
    broker = PaperBroker(settings)
    broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 115.0)

    import pandas as pd
    bar = pd.DataFrame(
        {"Open": [96], "High": [97], "Low": [94], "Close": [95], "Volume": [1]}
    )
    monkeypatch.setattr("execution.broker.fetch_ohlcv", lambda s, period="5d": bar)
    events = broker.poll_exits()
    assert len(events) == 1
    assert events[0].exit_reason == ExitReason.STOP_HIT
    assert broker.get_positions() == {}


def test_paper_broker_poll_exit_target_hit(settings: Settings, monkeypatch) -> None:
    broker = PaperBroker(settings)
    broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 115.0)

    import pandas as pd
    bar = pd.DataFrame(
        {"Open": [110], "High": [116], "Low": [109], "Close": [115], "Volume": [1]}
    )
    monkeypatch.setattr("execution.broker.fetch_ohlcv", lambda s, period="5d": bar)
    events = broker.poll_exits()
    assert len(events) == 1
    assert events[0].exit_reason == ExitReason.TARGET_HIT


# ---------------------------------------------------------------- exit manager

def test_exit_manager_finalises_broker_exit(settings: Settings, monkeypatch) -> None:
    broker = PaperBroker(settings)
    risk = RiskManager(settings)
    journal = TradeLogger(str(settings.DATA_DIR))

    order = _order("AAPL", 10)
    res = broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 115.0)
    journal.log_entry(order, res.fill_price)
    risk.register_position(order, res.fill_price)

    # Force a target hit on the next poll.
    import pandas as pd
    bar = pd.DataFrame(
        {"Open": [110], "High": [116], "Low": [109], "Close": [115], "Volume": [1]}
    )
    monkeypatch.setattr("execution.broker.fetch_ohlcv", lambda s, period="5d": bar)

    mgr = ExitManager(settings, broker, risk, journal)
    summary = mgr._check_broker_exits()
    assert summary == 1
    assert risk.get_open_positions() == {}
    # Journal exit was recorded.
    df = pd.read_csv(journal.csv_path)
    assert df.iloc[-1]["exit_reason"] == "TARGET_HIT"
