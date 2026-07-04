"""Tests for IBKRBroker advanced order types (limit/scale-in/MOC/partial)."""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta

import pytest

from config.settings import Settings
from execution.broker import IBKRBroker
from signals.signal_types import ExitReason


# --------------------------------------------------------------------------- #
# Extended fake ib_insync (adds LimitOrder / StopOrder / Order)
# --------------------------------------------------------------------------- #


class FakeOrderStatus:
    def __init__(self, status="Submitted", avgFillPrice=0.0):
        self.status = status
        self.avgFillPrice = avgFillPrice


class FakeOrder:
    _seq = 0

    def __init__(self, action="", quantity=0, orderType=""):
        FakeOrder._seq += 1
        self.orderId = FakeOrder._seq
        self.action = action
        self.totalQuantity = quantity
        self.orderType = orderType
        self.lmtPrice = 0.0
        self.auxPrice = 0.0
        self.tif = ""
        self.goodTillDate = ""


class FakeLimitOrder(FakeOrder):
    def __init__(self, action, quantity, lmtPrice):
        super().__init__(action, quantity, "LMT")
        self.lmtPrice = lmtPrice


class FakeStopOrder(FakeOrder):
    def __init__(self, action, quantity, stopPrice):
        super().__init__(action, quantity, "STP")
        self.auxPrice = stopPrice


class FakeGenericOrder(FakeOrder):
    def __init__(self, action="", totalQuantity=0, orderType=""):
        super().__init__(action, totalQuantity, orderType)


class FakeContract:
    def __init__(self, symbol):
        self.symbol = symbol


class FakeTrade:
    def __init__(self, contract, order):
        self.contract = contract
        self.order = order
        self.orderStatus = FakeOrderStatus()


class FakeIB:
    def __init__(self):
        self._connected = False
        self.placed = []
        self.cancelled = []

    def connect(self, host, port, clientId=1):
        self._connected = True

    def disconnect(self):
        self._connected = False

    def isConnected(self):
        return self._connected

    def qualifyContracts(self, *contracts):
        return list(contracts)

    def bracketOrder(self, action, quantity, limitPrice, takeProfitPrice, stopLossPrice):
        parent = FakeLimitOrder(action, quantity, limitPrice)
        take = FakeLimitOrder("SELL", quantity, takeProfitPrice)
        stop = FakeStopOrder("SELL", quantity, stopLossPrice)
        return [parent, take, stop]

    def placeOrder(self, contract, order):
        trade = FakeTrade(contract, order)
        self.placed.append(trade)
        return trade

    def cancelOrder(self, order):
        self.cancelled.append(order)

    def positions(self):
        return []


@pytest.fixture
def fake_ib(monkeypatch):
    mod = types.ModuleType("ib_insync")
    mod.IB = FakeIB
    mod.Stock = lambda symbol, exchange="SMART", currency="USD": FakeContract(symbol)
    mod.LimitOrder = FakeLimitOrder
    mod.StopOrder = FakeStopOrder
    mod.MarketOrder = lambda action, qty: FakeGenericOrder(action, qty, "MKT")
    mod.Order = FakeGenericOrder
    monkeypatch.setitem(sys.modules, "ib_insync", mod)
    return mod


@pytest.fixture
def broker(settings: Settings, fake_ib):
    b = IBKRBroker(settings)
    assert b.connect() is True
    return b


# --------------------------------------------------------------------------- #
# limit orders
# --------------------------------------------------------------------------- #


def test_limit_order_rests_with_gtd(broker: IBKRBroker) -> None:
    res = broker.place_limit_order("AAPL", 10, 99.0, 94.0, 110.0, expiry_hours=4.0)
    assert res.accepted and res.reason == "resting"
    entry = broker._pending_entries["AAPL"][0]
    assert entry["entry_trade"].order.tif == "GTD"
    assert entry["entry_trade"].order.goodTillDate  # populated


def test_limit_fill_attaches_bracket(broker: IBKRBroker) -> None:
    broker.place_limit_order("AAPL", 10, 99.0, 94.0, 110.0)
    # Mark the resting entry as filled.
    broker._pending_entries["AAPL"][0]["entry_trade"].orderStatus = FakeOrderStatus(
        "Filled", avgFillPrice=98.5
    )
    fills = broker.poll_pending_entries()
    assert len(fills) == 1 and fills[0].filled
    assert "AAPL" in broker._brackets
    bracket = broker._brackets["AAPL"]
    assert bracket["quantity"] == 10
    assert bracket["stop_trade"] is not None
    assert bracket["target_trade"] is not None
    assert "AAPL" not in broker._pending_entries


def test_limit_order_expires_and_cancels(broker: IBKRBroker) -> None:
    broker.place_limit_order("AAPL", 10, 99.0, 94.0, 110.0, expiry_hours=4.0)
    broker._pending_entries["AAPL"][0]["placed_at"] = (
        datetime.now() - timedelta(hours=5)
    ).isoformat()
    fills = broker.poll_pending_entries()
    assert len(fills) == 1 and fills[0].expired
    assert "AAPL" not in broker._pending_entries
    assert len(broker._ib.cancelled) == 1


# --------------------------------------------------------------------------- #
# scale-in
# --------------------------------------------------------------------------- #


def test_scale_in_places_multiple_and_averages(broker: IBKRBroker) -> None:
    tranches = [(100.0, 10), (99.0, 10), (98.0, 10)]
    results = broker.place_scale_in("AAPL", tranches, 94.0, 115.0)
    assert all(r.accepted for r in results)
    assert len(broker._pending_entries["AAPL"]) == 3

    # Fill the first tranche -> opens bracket at 100.
    broker._pending_entries["AAPL"][0]["entry_trade"].orderStatus = FakeOrderStatus(
        "Filled", avgFillPrice=100.0
    )
    broker.poll_pending_entries()
    assert broker._brackets["AAPL"]["quantity"] == 10

    # Fill the second tranche -> averages into the same position.
    broker._pending_entries["AAPL"][0]["entry_trade"].orderStatus = FakeOrderStatus(
        "Filled", avgFillPrice=98.0
    )
    broker.poll_pending_entries()
    assert broker._brackets["AAPL"]["quantity"] == 20
    assert broker._brackets["AAPL"]["entry_price"] == pytest.approx(99.0)


# --------------------------------------------------------------------------- #
# MOC
# --------------------------------------------------------------------------- #


def test_moc_order_submits_moc_type(broker: IBKRBroker) -> None:
    res = broker.place_moc_order("AAPL", 10, 94.0, 115.0)
    assert res.accepted and res.reason == "resting_moc"
    order = broker._pending_entries["AAPL"][0]["entry_trade"].order
    assert order.orderType == "MOC"


# --------------------------------------------------------------------------- #
# partial take on a bracket
# --------------------------------------------------------------------------- #


def test_bracket_arms_partial_take(broker: IBKRBroker) -> None:
    broker.place_bracket_order(
        "AAPL", 10, 100.0, 95.0, 130.0,
        partial_take_pct=0.5, partial_take_target_r=1.0,
    )
    bracket = broker._brackets["AAPL"]
    assert bracket["partial_trade"] is not None
    assert bracket["partial_take_qty"] == 5
    assert bracket["partial_take_price"] == pytest.approx(105.0)


def test_partial_leg_fill_trims_position(broker: IBKRBroker) -> None:
    broker.place_bracket_order(
        "AAPL", 10, 100.0, 95.0, 130.0,
        partial_take_pct=0.5, partial_take_target_r=1.0,
    )
    broker._brackets["AAPL"]["partial_trade"].orderStatus = FakeOrderStatus(
        "Filled", avgFillPrice=105.0
    )
    events = broker.poll_exits()
    assert len(events) == 1
    ev = events[0]
    assert ev.exit_reason == ExitReason.PARTIAL_TAKE
    assert ev.fill_details["quantity"] == 5
    # Position stays tracked with the runner.
    assert "AAPL" in broker._brackets
    assert broker._brackets["AAPL"]["quantity"] == 5
    assert broker._brackets["AAPL"]["partial_taken"] is True
    # A second poll does not re-take.
    assert broker.poll_exits() == []
