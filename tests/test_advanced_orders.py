"""Tests for advanced order execution (limit/scale-in/MOC/partial-take)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from config.settings import Settings
from execution.advanced_orders import (
    compute_scale_in_tranches,
    expiry_at,
    is_expired,
    partial_take_price,
    partial_take_split,
)
from execution.broker import PaperBroker
from execution.exit_manager import ExitManager
from journal.trade_logger import TradeLogger
from risk.manager import RiskManager
from signals.signal_types import ExitEvent, ExitReason, Signal, TradeOrder


# =========================================================================== #
# Pure helpers
# =========================================================================== #


def test_scale_in_even_split() -> None:
    tr = compute_scale_in_tranches(100.0, 30, 3, 0.01)
    assert [q for _, q in tr] == [10, 10, 10]
    assert [p for p, _ in tr] == [100.0, 99.0, 98.0]


def test_scale_in_remainder_on_first() -> None:
    tr = compute_scale_in_tranches(100.0, 32, 3, 0.01)
    assert [q for _, q in tr] == [12, 10, 10]
    assert sum(q for _, q in tr) == 32


def test_scale_in_single_tranche() -> None:
    assert compute_scale_in_tranches(100.0, 10, 1, 0.01) == [(100.0, 10)]


def test_scale_in_fewer_shares_than_tranches() -> None:
    assert compute_scale_in_tranches(100.0, 2, 3, 0.01) == [(100.0, 2)]


def test_scale_in_zero_quantity() -> None:
    assert compute_scale_in_tranches(100.0, 0, 3, 0.01) == []


def test_partial_take_split() -> None:
    assert partial_take_split(10, 0.5) == (5, 5)
    assert partial_take_split(11, 0.5) == (6, 5)  # rounds, clamps runner >= 1
    # too small / invalid pct -> keep whole
    assert partial_take_split(1, 0.5) == (0, 1)
    assert partial_take_split(10, 0.0) == (0, 10)
    assert partial_take_split(10, 1.0) == (0, 10)


def test_partial_take_price() -> None:
    # entry 100 stop 95 -> risk 5 -> 1R target 105
    assert partial_take_price(100.0, 95.0, 1.0) == 105.0
    assert partial_take_price(100.0, 95.0, 2.0) == 110.0
    assert partial_take_price(100.0, 100.0, 1.0) is None  # non-positive risk


def test_expiry_and_is_expired() -> None:
    t0 = datetime(2026, 7, 10, 10, 0, 0)
    assert expiry_at(t0, 4.0) == datetime(2026, 7, 10, 14, 0, 0)
    assert is_expired(t0, 4.0, now=datetime(2026, 7, 10, 15, 0, 0)) is True
    assert is_expired(t0, 4.0, now=datetime(2026, 7, 10, 13, 0, 0)) is False
    # zero expiry never expires (e.g. MOC), bad timestamp is fail-safe
    assert is_expired(t0, 0.0, now=datetime(2030, 1, 1)) is False
    assert is_expired("garbage", 4.0) is False


# =========================================================================== #
# PaperBroker — resting limit orders
# =========================================================================== #


def test_limit_order_fills_on_dip(settings: Settings, monkeypatch) -> None:
    broker = PaperBroker(settings)
    res = broker.place_limit_order("AAPL", 10, 99.0, 94.0, 110.0, expiry_hours=4.0)
    assert res.accepted and res.reason == "resting"
    assert broker.get_positions() == {}  # not filled yet

    # Price above the limit -> still resting.
    monkeypatch.setattr("execution.broker.fetch_current_price", lambda s: 100.0)
    assert broker.poll_pending_entries() == []
    assert broker.get_positions() == {}

    # Price dips to the limit -> fills.
    monkeypatch.setattr("execution.broker.fetch_current_price", lambda s: 98.5)
    fills = broker.poll_pending_entries()
    assert len(fills) == 1 and fills[0].filled
    assert fills[0].quantity == 10
    assert broker.get_positions() == {"AAPL": 10}


def test_limit_order_expires(settings: Settings, monkeypatch) -> None:
    broker = PaperBroker(settings)
    broker.place_limit_order("AAPL", 10, 99.0, 94.0, 110.0, expiry_hours=4.0)
    # Backdate the placement so it is now stale.
    broker._pending["AAPL"][0].placed_at = (
        datetime.now() - timedelta(hours=5)
    ).isoformat()
    monkeypatch.setattr("execution.broker.fetch_current_price", lambda s: 98.0)
    fills = broker.poll_pending_entries()
    assert len(fills) == 1 and fills[0].expired
    assert broker.get_positions() == {}
    assert broker.get_pending_orders() == {}


def test_pending_orders_persist(settings: Settings) -> None:
    b1 = PaperBroker(settings)
    b1.place_limit_order("AAPL", 10, 99.0, 94.0, 110.0)
    b2 = PaperBroker(settings)
    assert "AAPL" in b2.get_pending_orders()
    assert b2.get_pending_orders()["AAPL"][0]["quantity"] == 10


# =========================================================================== #
# PaperBroker — scale-in
# =========================================================================== #


def test_scale_in_averages_into_one_position(settings: Settings, monkeypatch) -> None:
    broker = PaperBroker(settings)
    tranches = compute_scale_in_tranches(100.0, 30, 3, 0.01)  # 100/99/98 x10
    results = broker.place_scale_in("AAPL", tranches, 94.0, 115.0, expiry_hours=8.0)
    assert all(r.accepted for r in results)
    assert len(broker.get_pending_orders()["AAPL"]) == 3

    # Price at 97.5 fills all three tranches (limits 100/99/98 all >= 97.5).
    monkeypatch.setattr("execution.broker.fetch_current_price", lambda s: 97.5)
    fills = broker.poll_pending_entries()
    assert sum(f.quantity for f in fills if f.filled) == 30
    assert broker.get_positions() == {"AAPL": 30}
    # Entry averaged across the three fills (all filled at 97.5 + slippage).
    detail = broker.get_position_detail("AAPL")
    assert detail["quantity"] == 30
    assert detail["entry_price"] == pytest.approx(97.5 * 1.0005, abs=0.01)


# =========================================================================== #
# PaperBroker — market-on-close
# =========================================================================== #


def test_moc_fills_at_market(settings: Settings, monkeypatch) -> None:
    broker = PaperBroker(settings)
    res = broker.place_moc_order("AAPL", 10, 94.0, 115.0)
    assert res.accepted and res.reason == "resting_moc"
    monkeypatch.setattr("execution.broker.fetch_current_price", lambda s: 101.0)
    fills = broker.poll_pending_entries()
    assert len(fills) == 1 and fills[0].filled
    assert broker.get_positions() == {"AAPL": 10}
    assert broker.get_position_detail("AAPL")["entry_price"] == pytest.approx(
        101.0 * 1.0005, abs=0.01
    )


# =========================================================================== #
# PaperBroker — partial take
# =========================================================================== #


def _bar(o, h, l, c):
    return pd.DataFrame({"Open": [o], "High": [h], "Low": [l], "Close": [c],
                         "Volume": [1]})


def test_partial_take_trims_then_runs(settings: Settings, monkeypatch) -> None:
    broker = PaperBroker(settings)
    # entry 100.05 (after slippage), stop 95, partial 50% at 1R.
    broker.place_bracket_order(
        "AAPL", 10, 100.0, 95.0, 130.0,
        partial_take_pct=0.5, partial_take_target_r=1.0,
    )
    detail = broker.get_position_detail("AAPL")
    # 1R target = entry + (entry-stop). entry ~100.05 -> ~105.1
    assert detail["partial_take_price"] == pytest.approx(105.1, abs=0.2)

    # Bar reaches the partial target but not the final target -> partial take.
    monkeypatch.setattr("execution.broker.fetch_ohlcv",
                        lambda s, period="5d": _bar(104, 106, 103, 105))
    events = broker.poll_exits()
    assert len(events) == 1
    assert events[0].exit_reason == ExitReason.PARTIAL_TAKE
    assert events[0].fill_details["quantity"] == 5
    # Position still open with the runner (5 shares).
    assert broker.get_positions() == {"AAPL": 5}
    assert broker.get_position_detail("AAPL")["partial_taken"] is True

    # A second poll at the same level does NOT take again.
    assert broker.poll_exits() == []

    # Runner reaches the final target -> full close of remaining 5.
    monkeypatch.setattr("execution.broker.fetch_ohlcv",
                        lambda s, period="5d": _bar(129, 131, 128, 130))
    events = broker.poll_exits()
    assert len(events) == 1
    assert events[0].exit_reason == ExitReason.TARGET_HIT
    assert events[0].fill_details["quantity"] == 5
    assert broker.get_positions() == {}


def test_partial_take_not_armed_without_pct(settings: Settings, monkeypatch) -> None:
    broker = PaperBroker(settings)
    broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 130.0)
    assert broker.get_position_detail("AAPL")["partial_take_pct"] == 0.0


# =========================================================================== #
# Journal partial-exit accounting + risk reduce
# =========================================================================== #


def _order(symbol="AAPL", qty=10):
    sig = Signal(symbol=symbol, strategy="momentum", entry_price=100.0,
                 stop_price=95.0, target_price=130.0, signal_strength=0.8)
    return TradeOrder(signal=sig, quantity=qty, currency="USD",
                      ai_decision="APPROVE", ai_reasoning="ok")


def test_log_partial_exit_splits_row(settings: Settings) -> None:
    journal = TradeLogger(str(settings.DATA_DIR))
    journal.log_entry(_order("AAPL", 10), 100.0, commission=0.05)

    ev = ExitEvent(
        symbol="AAPL", exit_price=110.0, exit_reason=ExitReason.PARTIAL_TAKE,
        fill_details={"quantity": 5, "commission": 0.02},
    )
    journal.log_partial_exit("AAPL", ev, exit_commission=0.02)

    df = pd.read_csv(journal.csv_path)
    # Two rows now: the shrunk-open runner (qty 5, no exit) + closed partial.
    open_rows = df[df["exit_time"].isna() | (df["exit_time"] == "")]
    closed_rows = df[df["exit_time"].notna() & (df["exit_time"] != "")]
    assert len(open_rows) == 1 and int(open_rows.iloc[0]["quantity"]) == 5
    assert len(closed_rows) == 1
    partial = closed_rows.iloc[0]
    assert partial["exit_reason"] == "PARTIAL_TAKE"
    assert int(partial["quantity"]) == 5
    assert float(partial["pnl_gross"]) == pytest.approx((110.0 - 100.0) * 5)


def test_risk_reduce_position(settings: Settings) -> None:
    risk = RiskManager(settings)
    risk.register_position(_order("AAPL", 10), 100.0)
    assert risk.reduce_position("AAPL", 4) is True
    assert risk.get_open_positions()["AAPL"]["quantity"] == 6
    assert risk.reduce_position("ZZZZ", 1) is False


def test_risk_register_averages_scale_in(settings: Settings) -> None:
    risk = RiskManager(settings)
    risk.register_position(_order("AAPL", 10), 100.0, quantity=10)
    risk.register_position(_order("AAPL", 10), 98.0, quantity=10)
    pos = risk.get_open_positions()["AAPL"]
    assert pos["quantity"] == 20
    assert pos["entry_price"] == pytest.approx(99.0)


# =========================================================================== #
# Exit manager end-to-end partial take
# =========================================================================== #


def test_exit_manager_finalises_partial(settings: Settings, monkeypatch) -> None:
    broker = PaperBroker(settings)
    risk = RiskManager(settings)
    journal = TradeLogger(str(settings.DATA_DIR))

    order = _order("AAPL", 10)
    broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 130.0,
                               partial_take_pct=0.5, partial_take_target_r=1.0)
    journal.log_entry(order, 100.05)
    risk.register_position(order, 100.05)

    monkeypatch.setattr("execution.broker.fetch_ohlcv",
                        lambda s, period="5d": _bar(104, 106, 103, 105))
    mgr = ExitManager(settings, broker, risk, journal)
    n = mgr._check_broker_exits()
    assert n == 1
    # Position NOT removed; quantity reduced to runner.
    assert risk.get_open_positions()["AAPL"]["quantity"] == 5
    # Daily P&L recorded for the partial slice.
    assert risk.daily_pnl > 0
