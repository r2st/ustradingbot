"""Tests for the paper-broker slippage, gap-through, and commission model."""

from __future__ import annotations

import pandas as pd
import pytest

from config.settings import Settings
from execution.broker import (
    PaperBroker,
    apply_entry_slippage,
    commission_for,
    stop_exit_fill,
    target_exit_fill,
)
from signals.signal_types import ExitReason


# ---------------------------------------------------------------- pure helpers


def test_apply_entry_slippage_is_adverse() -> None:
    # 5 bp on 100 -> 100.05 (buy fills higher).
    assert apply_entry_slippage(100.0, 5.0) == pytest.approx(100.05)
    assert apply_entry_slippage(100.0, 0.0) == 100.0


def test_stop_exit_fill_normal_applies_slippage() -> None:
    # Bar opened above the stop -> fill at stop minus slippage.
    assert stop_exit_fill(95.0, 96.0, 10.0) == pytest.approx(95.0 * (1 - 0.001))


def test_stop_exit_fill_gap_through_uses_open() -> None:
    # Bar gapped open below the stop -> fill at the (worse) open, not the stop.
    assert stop_exit_fill(95.0, 90.0, 5.0) == 90.0


def test_target_exit_fill_gap_up_is_favourable() -> None:
    # Gapped open above the target -> better fill at the open.
    assert target_exit_fill(115.0, 118.0) == 118.0
    # Normal touch -> fill at the target exactly (limit sell, no penalty).
    assert target_exit_fill(115.0, 110.0) == 115.0


def test_commission_for() -> None:
    assert commission_for(100, 0.005) == pytest.approx(0.5)
    assert commission_for(0, 0.005) == 0.0


# ---------------------------------------------------------------- broker wiring


def _broker(tmp_path, **overrides) -> PaperBroker:
    settings = Settings(DATA_DIR=tmp_path / "ds", **overrides)
    return PaperBroker(settings)


def test_entry_fill_includes_slippage_and_commission(tmp_path) -> None:
    broker = _broker(tmp_path, PAPER_SLIPPAGE_BPS=10.0, PAPER_COMMISSION_PER_SHARE=0.01)
    res = broker.place_bracket_order("AAPL", 20, 100.0, 95.0, 115.0)
    assert res.fill_price == pytest.approx(100.10)  # 10 bp
    assert res.commission == pytest.approx(20 * 0.01)
    # Stored position reflects the slipped entry.
    assert broker.get_position_detail("AAPL")["entry_price"] == pytest.approx(100.10)


def test_zero_slippage_zero_commission(tmp_path) -> None:
    broker = _broker(tmp_path, PAPER_SLIPPAGE_BPS=0.0, PAPER_COMMISSION_PER_SHARE=0.0)
    res = broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 115.0)
    assert res.fill_price == 100.0
    assert res.commission == 0.0


def _bar(o, h, l, c) -> pd.DataFrame:
    return pd.DataFrame({"Open": [o], "High": [h], "Low": [l], "Close": [c], "Volume": [1]})


def test_poll_stop_hit_applies_slippage(tmp_path, monkeypatch) -> None:
    broker = _broker(tmp_path, PAPER_SLIPPAGE_BPS=20.0, PAPER_COMMISSION_PER_SHARE=0.005)
    broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 115.0)
    # Bar dips to the stop but opens above it -> stop minus 20 bp.
    monkeypatch.setattr(
        "execution.broker.fetch_ohlcv", lambda s, period="5d": _bar(96, 97, 94, 95.5)
    )
    events = broker.poll_exits()
    assert len(events) == 1
    ev = events[0]
    assert ev.exit_reason == ExitReason.STOP_HIT
    assert ev.exit_price == pytest.approx(95.0 * (1 - 0.002))
    assert ev.fill_details["commission"] == pytest.approx(10 * 0.005)


def test_poll_stop_gap_through_fills_at_open(tmp_path, monkeypatch) -> None:
    broker = _broker(tmp_path, PAPER_SLIPPAGE_BPS=5.0)
    broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 115.0)
    # Bar gaps open to 90 (below the 95 stop) -> fill at 90, not 95.
    monkeypatch.setattr(
        "execution.broker.fetch_ohlcv", lambda s, period="5d": _bar(90, 92, 89, 91)
    )
    events = broker.poll_exits()
    assert events[0].exit_reason == ExitReason.STOP_HIT
    assert events[0].exit_price == 90.0


def test_poll_target_gap_up_fills_better(tmp_path, monkeypatch) -> None:
    broker = _broker(tmp_path, PAPER_SLIPPAGE_BPS=5.0)
    broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 115.0)
    # Bar gaps open to 118 (above the 115 target) -> fill at 118.
    monkeypatch.setattr(
        "execution.broker.fetch_ohlcv", lambda s, period="5d": _bar(118, 120, 117, 119)
    )
    events = broker.poll_exits()
    assert events[0].exit_reason == ExitReason.TARGET_HIT
    assert events[0].exit_price == 118.0


def test_net_pnl_accounts_for_round_trip_commission(tmp_path, monkeypatch) -> None:
    """Entry+exit commission must reduce the journaled net P&L."""
    from journal.trade_logger import TradeLogger
    from risk.manager import RiskManager
    from execution.exit_manager import ExitManager
    from signals.signal_types import Signal, TradeOrder

    settings = Settings(
        DATA_DIR=tmp_path / "ds",
        PAPER_SLIPPAGE_BPS=0.0,
        PAPER_COMMISSION_PER_SHARE=0.01,
    )
    broker = PaperBroker(settings)
    risk = RiskManager(settings)
    journal = TradeLogger(str(settings.DATA_DIR))

    sig = Signal(symbol="AAPL", strategy="momentum", entry_price=100.0,
                 stop_price=95.0, target_price=115.0, signal_strength=0.8)
    order = TradeOrder(signal=sig, quantity=10, currency="USD",
                       ai_decision="APPROVE", ai_reasoning="ok")
    res = broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 115.0)
    journal.log_entry(order, res.fill_price, res.commission)
    risk.register_position(order, res.fill_price)

    # Opens below the target then reaches it -> fills at exactly 115.
    monkeypatch.setattr(
        "execution.broker.fetch_ohlcv", lambda s, period="5d": _bar(114, 116, 113, 115)
    )
    mgr = ExitManager(settings, broker, risk, journal)
    assert mgr._check_broker_exits() == 1

    df = pd.read_csv(journal.csv_path)
    row = df.iloc[-1]
    # gross = (115 - 100) * 10 = 150; commissions = 0.10 entry + 0.10 exit.
    assert float(row["pnl_gross"]) == pytest.approx(150.0)
    assert float(row["entry_commission"]) == pytest.approx(0.10)
    assert float(row["exit_commission"]) == pytest.approx(0.10)
    assert float(row["pnl_net"]) == pytest.approx(150.0 - 0.20)
