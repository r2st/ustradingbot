"""Tests for manual trade entry (feature 2)."""

from __future__ import annotations

import pytest

from config.settings import Settings
from execution.manual_trade import (
    ManualTradeError,
    build_manual_order,
    place_manual_trade,
    validate_params,
)


def _params(**kw):
    base = dict(symbol="AAPL", entry_price=100, stop_price=95, target_price=110, quantity=10)
    base.update(kw)
    return base


def test_validate_ok():
    clean = validate_params(_params())
    assert clean["symbol"] == "AAPL" and clean["quantity"] == 10


@pytest.mark.parametrize("bad", [
    dict(symbol=""),
    dict(quantity=0),
    dict(quantity=-5),
    dict(stop_price=100),   # stop == entry
    dict(stop_price=105),   # stop > entry
    dict(target_price=100),  # target == entry
    dict(target_price=90),   # target < entry
    dict(entry_price="x"),
])
def test_validate_rejects(bad):
    with pytest.raises(ManualTradeError):
        validate_params(_params(**bad))


def test_build_order_currency_and_risk():
    order = build_manual_order(_params(symbol="SHOP.TO"))
    assert order.currency == "CAD"
    assert order.risk_amount == 50.0  # (100-95)*10
    assert order.signal.raw_data.get("manual") is True


class _FakeBracket:
    accepted = True
    fill_price = 100.5
    commission = 0.05
    order_id = "OID1"
    reason = "filled"


class _FakeBroker:
    def __init__(self, accept=True):
        self._accept = accept
        self.calls = []

    def is_connected(self):
        return True

    def connect(self):
        return True

    def place_bracket_order(self, **kw):
        self.calls.append(kw)
        r = _FakeBracket()
        r.accepted = self._accept
        return r


class _FakeLogger:
    def __init__(self):
        self.entries = []

    def log_entry(self, order, fill_price, commission):
        self.entries.append((order.signal.symbol, fill_price))


class _FakeRisk:
    def __init__(self):
        self.registered = []

    def register_position(self, order, fill_price):
        self.registered.append((order.signal.symbol, fill_price))


def test_place_manual_trade_success():
    broker, logger, risk = _FakeBroker(), _FakeLogger(), _FakeRisk()
    res = place_manual_trade(_params(), Settings(), broker=broker,
                             trade_logger=logger, risk_manager=risk)
    assert res.ok
    assert res.fill_price == 100.5
    assert broker.calls[0]["symbol"] == "AAPL"
    assert logger.entries and risk.registered


def test_place_manual_trade_validation_fails_no_broker_call():
    broker = _FakeBroker()
    res = place_manual_trade(_params(quantity=0), Settings(), broker=broker,
                             trade_logger=_FakeLogger(), risk_manager=_FakeRisk())
    assert not res.ok
    assert broker.calls == []


def test_place_manual_trade_broker_reject():
    broker = _FakeBroker(accept=False)
    res = place_manual_trade(_params(), Settings(), broker=broker,
                             trade_logger=_FakeLogger(), risk_manager=_FakeRisk())
    assert not res.ok and "rejected" in res.message.lower()
