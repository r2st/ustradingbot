"""Tests for the IBKRBroker live-path exit logic (P0 fix).

The real ``ib_insync`` package is optional and needs a running TWS/Gateway, so
these tests inject a lightweight fake ``ib_insync`` module into ``sys.modules``
and drive the broker through it.  They cover the three formerly-stubbed
methods: :meth:`poll_exits`, :meth:`modify_stop`, and :meth:`force_close`,
plus :meth:`is_connected`.
"""

from __future__ import annotations

import sys
import types

import pytest

from config.settings import Settings
from execution.broker import IBKRBroker
from signals.signal_types import ExitReason


# --------------------------------------------------------------------------- #
# Fake ib_insync objects
# --------------------------------------------------------------------------- #


class FakeOrderStatus:
    def __init__(self, status: str = "Submitted", avgFillPrice: float = 0.0) -> None:
        self.status = status
        self.avgFillPrice = avgFillPrice


class FakeOrder:
    def __init__(self, orderId: int = 0, action: str = "", quantity: int = 0,
                 orderType: str = "") -> None:
        self.orderId = orderId
        self.action = action
        self.totalQuantity = quantity
        self.orderType = orderType
        self.lmtPrice = 0.0
        self.auxPrice = 0.0


class FakeMarketOrder(FakeOrder):
    def __init__(self, action: str, quantity: int) -> None:
        super().__init__(orderId=9999, action=action, quantity=quantity, orderType="MKT")


class FakeTrade:
    def __init__(self, contract: "FakeContract", order: FakeOrder) -> None:
        self.contract = contract
        self.order = order
        self.orderStatus = FakeOrderStatus()


class FakeContract:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol


class FakeIB:
    """Minimal stand-in for ib_insync.IB used by IBKRBroker."""

    def __init__(self) -> None:
        self._next_id = 1
        self._connected = False
        self.cancelled: list = []
        self.placed: list = []

    def connect(self, host: str, port: int, clientId: int = 1) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def isConnected(self) -> bool:
        return self._connected

    def qualifyContracts(self, *contracts):
        return list(contracts)

    def bracketOrder(self, action, quantity, limitPrice, takeProfitPrice,
                     stopLossPrice):
        parent = FakeOrder(self._next_id, action, quantity, "LMT")
        parent.lmtPrice = limitPrice
        take = FakeOrder(self._next_id + 1, "SELL", quantity, "LMT")
        take.lmtPrice = takeProfitPrice
        stop = FakeOrder(self._next_id + 2, "SELL", quantity, "STP")
        stop.auxPrice = stopLossPrice
        self._next_id += 3
        return [parent, take, stop]

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        return FakeTrade(contract, order)

    def cancelOrder(self, order):
        self.cancelled.append(order)

    def positions(self):
        return []


@pytest.fixture
def fake_ib_module(monkeypatch):
    """Install a fake ``ib_insync`` module for the duration of a test."""
    mod = types.ModuleType("ib_insync")
    mod.IB = FakeIB
    mod.Stock = lambda symbol, exchange="SMART", currency="USD": FakeContract(symbol)
    mod.MarketOrder = FakeMarketOrder
    monkeypatch.setitem(sys.modules, "ib_insync", mod)
    return mod


@pytest.fixture
def ib_broker(settings: Settings, fake_ib_module):
    broker = IBKRBroker(settings)
    assert broker.connect() is True
    return broker


def _place(broker: IBKRBroker, symbol: str = "AAPL", qty: int = 10):
    return broker.place_bracket_order(symbol, qty, 100.0, 95.0, 115.0)


# --------------------------------------------------------------------------- #
# connection
# --------------------------------------------------------------------------- #


def test_is_connected_reflects_session(ib_broker: IBKRBroker) -> None:
    assert ib_broker.is_connected() is True
    ib_broker.disconnect()
    assert ib_broker.is_connected() is False


def test_not_connected_guards(settings: Settings) -> None:
    broker = IBKRBroker(settings)  # never connected -> _ib is None
    assert broker.is_connected() is False
    assert broker.poll_exits() == []
    assert broker.modify_stop("AAPL", 99.0) is False
    assert broker.force_close("AAPL", ExitReason.MANUAL) is None
    assert broker.get_positions() == {}


# --------------------------------------------------------------------------- #
# place + track
# --------------------------------------------------------------------------- #


def test_place_bracket_tracks_legs(ib_broker: IBKRBroker) -> None:
    res = _place(ib_broker)
    assert res.accepted
    assert "AAPL" in ib_broker._brackets
    bracket = ib_broker._brackets["AAPL"]
    assert bracket["quantity"] == 10
    assert bracket["stop_price"] == 95.0
    assert bracket["target_price"] == 115.0
    assert bracket["stop_trade"] is not None
    assert bracket["target_trade"] is not None


# --------------------------------------------------------------------------- #
# poll_exits
# --------------------------------------------------------------------------- #


def test_poll_exits_empty_when_unfilled(ib_broker: IBKRBroker) -> None:
    _place(ib_broker)
    assert ib_broker.poll_exits() == []
    assert "AAPL" in ib_broker._brackets  # still tracked


def test_poll_exits_detects_stop_hit(ib_broker: IBKRBroker) -> None:
    _place(ib_broker)
    stop_trade = ib_broker._brackets["AAPL"]["stop_trade"]
    stop_trade.orderStatus = FakeOrderStatus("Filled", avgFillPrice=95.0)

    events = ib_broker.poll_exits()
    assert len(events) == 1
    ev = events[0]
    assert ev.symbol == "AAPL"
    assert ev.exit_reason == ExitReason.STOP_HIT
    assert ev.exit_price == 95.0
    assert ev.pnl_gross == pytest.approx((95.0 - 100.0) * 10)
    # Bracket dropped and the sibling (target) leg was cancelled.
    assert "AAPL" not in ib_broker._brackets
    assert len(ib_broker._ib.cancelled) == 1


def test_poll_exits_detects_target_hit(ib_broker: IBKRBroker) -> None:
    _place(ib_broker)
    target_trade = ib_broker._brackets["AAPL"]["target_trade"]
    target_trade.orderStatus = FakeOrderStatus("Filled", avgFillPrice=115.0)

    events = ib_broker.poll_exits()
    assert len(events) == 1
    assert events[0].exit_reason == ExitReason.TARGET_HIT
    assert events[0].pnl_gross == pytest.approx((115.0 - 100.0) * 10)


def test_poll_exits_stop_takes_precedence(ib_broker: IBKRBroker) -> None:
    """When both legs report filled, the stop wins (conservative)."""
    _place(ib_broker)
    bracket = ib_broker._brackets["AAPL"]
    bracket["stop_trade"].orderStatus = FakeOrderStatus("Filled", avgFillPrice=95.0)
    bracket["target_trade"].orderStatus = FakeOrderStatus("Filled", avgFillPrice=115.0)

    events = ib_broker.poll_exits()
    assert len(events) == 1
    assert events[0].exit_reason == ExitReason.STOP_HIT


def test_poll_exits_falls_back_to_stop_price(ib_broker: IBKRBroker) -> None:
    """A fill with no avgFillPrice uses the tracked stop price."""
    _place(ib_broker)
    ib_broker._brackets["AAPL"]["stop_trade"].orderStatus = FakeOrderStatus(
        "Filled", avgFillPrice=0.0
    )
    events = ib_broker.poll_exits()
    assert events[0].exit_price == 95.0


# --------------------------------------------------------------------------- #
# modify_stop
# --------------------------------------------------------------------------- #


def test_modify_stop_moves_up(ib_broker: IBKRBroker) -> None:
    _place(ib_broker)
    assert ib_broker.modify_stop("AAPL", 98.0) is True
    assert ib_broker._brackets["AAPL"]["stop_price"] == 98.0
    # The stop order was re-placed with the new trigger price.
    stop_order = ib_broker._brackets["AAPL"]["stop_trade"].order
    assert stop_order.auxPrice == 98.0


def test_modify_stop_rejects_lower(ib_broker: IBKRBroker) -> None:
    _place(ib_broker)
    assert ib_broker.modify_stop("AAPL", 90.0) is False  # below current 95
    assert ib_broker._brackets["AAPL"]["stop_price"] == 95.0


def test_modify_stop_unknown_symbol(ib_broker: IBKRBroker) -> None:
    assert ib_broker.modify_stop("ZZZZ", 99.0) is False


# --------------------------------------------------------------------------- #
# force_close
# --------------------------------------------------------------------------- #


def test_force_close_cancels_legs_and_sells(ib_broker: IBKRBroker, monkeypatch) -> None:
    _place(ib_broker)
    monkeypatch.setattr("execution.broker.fetch_current_price", lambda s: 105.0)

    event = ib_broker.force_close("AAPL", ExitReason.TIME_EXIT_ZOMBIE)
    assert event is not None
    assert event.exit_reason == ExitReason.TIME_EXIT_ZOMBIE
    assert event.exit_price == 105.0
    assert event.pnl_gross == pytest.approx((105.0 - 100.0) * 10)
    assert "AAPL" not in ib_broker._brackets
    # Both resting legs cancelled and a SELL market order placed.
    assert len(ib_broker._ib.cancelled) == 2
    assert ib_broker._ib.placed[-1][1].action == "SELL"
    assert ib_broker._ib.placed[-1][1].orderType == "MKT"


def test_force_close_uses_market_fill_when_available(ib_broker: IBKRBroker, monkeypatch) -> None:
    _place(ib_broker)

    # Make the market order report an immediate fill at 107.
    def _place_order(contract, order):
        trade = FakeTrade(contract, order)
        if getattr(order, "orderType", "") == "MKT":
            trade.orderStatus = FakeOrderStatus("Filled", avgFillPrice=107.0)
        ib_broker._ib.placed.append((contract, order))
        return trade

    monkeypatch.setattr(ib_broker._ib, "placeOrder", _place_order)
    event = ib_broker.force_close("AAPL", ExitReason.MANUAL)
    assert event.exit_price == 107.0


def test_force_close_unknown_symbol(ib_broker: IBKRBroker) -> None:
    assert ib_broker.force_close("ZZZZ", ExitReason.MANUAL) is None
