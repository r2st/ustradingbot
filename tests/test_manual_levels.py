"""Tests for multi-level / sell manual trades end-to-end (feature 2).

Covers the extended validation, the order-routing split (simple long bracket
vs. the level engine), and the PaperBroker's laddered exit simulation for
both long and short positions.
"""

from __future__ import annotations

import pandas as pd
import pytest

import execution.broker as broker_mod
from execution.broker import PaperBroker
from execution.manual_trade import (
    ManualTradeError,
    build_manual_order,
    place_manual_trade,
    validate_params,
)
from risk.manager import RiskManager
from signals.signal_types import ExitReason


def _params(**kw):
    base = dict(
        symbol="AAPL", side="buy", entry_price=100, quantity=90,
        stops=[{"percent": 3, "pct": 33}, {"percent": 5, "pct": 33}, {"percent": 8}],
        targets=[{"percent": 5, "pct": 33}, {"percent": 10, "pct": 33}, {"percent": 15}],
    )
    base.update(kw)
    return base


# ------------------------------------------------------------- validation


def test_validate_multi_level_long():
    clean = validate_params(_params())
    assert clean["side"] == "long"
    assert clean["stop_price"] == 97.0       # nearest stop
    assert clean["target_price"] == 105.0    # nearest target
    assert len(clean["levels"]) == 6


def test_validate_sell_side_short():
    clean = validate_params(_params(
        side="sell",
        stops=[{"percent": 3}],
        targets=[{"percent": 5, "pct": 50}, {"percent": 10}],
    ))
    assert clean["side"] == "short"
    assert clean["stop_price"] == 103.0
    assert clean["target_price"] == 95.0


def test_validate_rejects_wrong_side_ladder():
    with pytest.raises(ManualTradeError):
        validate_params(_params(stops=[{"price": 105}]))  # stop above entry, long
    with pytest.raises(ManualTradeError):
        validate_params(_params(side="sell", targets=[{"price": 110}]))


def test_build_order_ladder_risk_is_worst_case():
    order = build_manual_order(_params(quantity=90))
    # stops: 29 @ -3, 29 @ -5, 32 @ -8  →  29*3 + 29*5 + 32*8
    stops = [l for l in order.signal.raw_data["levels"] if l["kind"] == "stop"]
    expected = sum((100 - l["price"]) * l["quantity"] for l in stops)
    assert order.risk_amount == round(expected, 2)
    assert order.signal.direction == "long"
    assert order.signal.raw_data["manual"] is True


# ----------------------------------------------------------- order routing


class _FakeBracket:
    accepted = True
    fill_price = 100.5
    commission = 0.05
    order_id = "OID1"
    reason = "filled"


class _FakeBroker:
    def __init__(self):
        self.bracket_calls, self.manual_calls = [], []

    def is_connected(self):
        return True

    def connect(self):
        return True

    def place_bracket_order(self, **kw):
        self.bracket_calls.append(kw)
        return _FakeBracket()

    def place_manual_bracket(self, **kw):
        self.manual_calls.append(kw)
        return _FakeBracket()


class _FakeLogger:
    def __init__(self):
        self.entries = []

    def log_entry(self, order, fill_price, commission):
        self.entries.append((order.signal.symbol, fill_price))


class _FakeRisk:
    def __init__(self):
        self.registered = []

    def pre_check(self, signal):
        return True, ""

    def register_position(self, order, fill_price):
        self.registered.append((order.signal.symbol, fill_price))


def test_simple_long_uses_classic_bracket(settings):
    broker = _FakeBroker()
    res = place_manual_trade(
        dict(symbol="AAPL", entry_price=100, stop_price=95, target_price=110,
             quantity=10),
        settings, broker=broker, trade_logger=_FakeLogger(), risk_manager=_FakeRisk(),
    )
    assert res.ok
    assert broker.bracket_calls and not broker.manual_calls


def test_multi_level_uses_level_engine(settings):
    broker, logger, risk = _FakeBroker(), _FakeLogger(), _FakeRisk()
    res = place_manual_trade(_params(), settings, broker=broker,
                             trade_logger=logger, risk_manager=risk)
    assert res.ok
    assert broker.manual_calls and not broker.bracket_calls
    call = broker.manual_calls[0]
    assert call["side"] == "long" and len(call["levels"]) == 6
    assert logger.entries and risk.registered


def test_short_uses_level_engine_even_single_level(settings):
    broker = _FakeBroker()
    res = place_manual_trade(
        dict(symbol="AAPL", side="sell", entry_price=100, quantity=10,
             stops=[{"percent": 3}], targets=[{"percent": 5}]),
        settings, broker=broker, trade_logger=_FakeLogger(), risk_manager=_FakeRisk(),
    )
    assert res.ok
    assert broker.manual_calls and not broker.bracket_calls
    assert "Sold short" in res.message


# --------------------------------------------- PaperBroker level simulation


def _bar(open_, low, high):
    """Monkeypatch-able single daily bar."""
    return pd.DataFrame(
        {"Open": [open_], "Low": [low], "High": [high], "Close": [open_],
         "Volume": [1_000_000]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-07-06")]),
    )


@pytest.fixture
def paper(settings):
    settings = settings.model_copy(update={"PAPER_SLIPPAGE_BPS": 0.0,
                                           "PAPER_COMMISSION_PER_SHARE": 0.0})
    return PaperBroker(settings)


def _place_long(paper):
    from execution.levels import build_levels

    levels = build_levels(
        "buy", 100.0, 90,
        [{"price": 97, "pct": 33}, {"price": 95, "pct": 33}, {"price": 92}],
        [{"price": 105, "pct": 33}, {"price": 110, "pct": 33}, {"price": 115}],
    )
    res = paper.place_manual_bracket(
        "AAPL", "long", 90, 100.0, [l.to_dict() for l in levels]
    )
    assert res.accepted
    return res


def test_paper_long_partial_stop_then_full_stop(paper, monkeypatch):
    _place_long(paper)

    # Day 1: dips to 96.5 — only the first stop (97) triggers, partial exit.
    monkeypatch.setattr(broker_mod, "fetch_ohlcv", lambda *a, **k: _bar(99, 96.5, 100))
    monkeypatch.setattr(broker_mod, "fetch_current_price", lambda s: 96.5)
    events = paper.poll_exits()
    assert len(events) == 1
    ev = events[0]
    assert ev.exit_reason == ExitReason.PARTIAL_TAKE
    assert ev.fill_details["quantity"] == 29  # 33% of 90
    assert ev.exit_price == 97.0
    assert paper.get_positions()["AAPL"] == 61

    # Day 2: crashes through 95 AND 92 — both remaining stops fire; the last
    # one empties the position and closes it as STOP_HIT.
    monkeypatch.setattr(broker_mod, "fetch_ohlcv", lambda *a, **k: _bar(94, 91, 94))
    monkeypatch.setattr(broker_mod, "fetch_current_price", lambda s: 91.0)
    events = paper.poll_exits()
    assert [e.exit_reason for e in events] == [
        ExitReason.PARTIAL_TAKE, ExitReason.STOP_HIT,
    ]
    assert "AAPL" not in paper.get_positions()


def test_paper_long_targets_scale_out(paper, monkeypatch):
    _place_long(paper)

    monkeypatch.setattr(broker_mod, "fetch_ohlcv", lambda *a, **k: _bar(104, 103, 106))
    monkeypatch.setattr(broker_mod, "fetch_current_price", lambda s: 104.0)
    events = paper.poll_exits()
    assert len(events) == 1
    assert events[0].exit_reason == ExitReason.PARTIAL_TAKE
    assert events[0].exit_price == 105.0
    assert events[0].pnl_gross == pytest.approx((105 - 100) * 29)

    monkeypatch.setattr(broker_mod, "fetch_ohlcv", lambda *a, **k: _bar(112, 111, 116))
    monkeypatch.setattr(broker_mod, "fetch_current_price", lambda s: 112.0)
    events = paper.poll_exits()
    # 110 and 115 both reached: partial then the closing TARGET_HIT.
    assert [e.exit_reason for e in events] == [
        ExitReason.PARTIAL_TAKE, ExitReason.TARGET_HIT,
    ]
    assert "AAPL" not in paper.get_positions()


def test_paper_stop_checked_before_target_same_bar(paper, monkeypatch):
    _place_long(paper)
    # Wild bar touching both the first stop and the first target: the stop
    # slice exits first (conservative), then the target slice for the rest.
    monkeypatch.setattr(broker_mod, "fetch_ohlcv", lambda *a, **k: _bar(100, 96.9, 105.1))
    monkeypatch.setattr(broker_mod, "fetch_current_price", lambda s: 100.0)
    events = paper.poll_exits()
    assert events[0].exit_reason == ExitReason.PARTIAL_TAKE
    assert events[0].fill_details["level"].startswith("stop@97")


def test_paper_short_ladder(paper, monkeypatch):
    from execution.levels import build_levels

    levels = build_levels(
        "sell", 100.0, 60,
        [{"price": 103, "pct": 50}, {"price": 105}],
        [{"price": 95, "pct": 50}, {"price": 90}],
    )
    res = paper.place_manual_bracket(
        "TSLA", "short", 60, 100.0, [l.to_dict() for l in levels]
    )
    assert res.accepted and res.fill_price == 100.0

    # Price falls to 94: first target (95) triggers — profit on a short.
    monkeypatch.setattr(broker_mod, "fetch_ohlcv", lambda *a, **k: _bar(96, 94, 97))
    monkeypatch.setattr(broker_mod, "fetch_current_price", lambda s: 96.0)
    events = paper.poll_exits()
    assert len(events) == 1
    assert events[0].exit_reason == ExitReason.PARTIAL_TAKE
    assert events[0].pnl_gross == pytest.approx((100 - 95) * 30)

    # Price rips to 106: the first stop's 30-share slice covers everything
    # that remains, so one STOP_HIT closes the short at a loss.
    monkeypatch.setattr(broker_mod, "fetch_ohlcv", lambda *a, **k: _bar(102, 101, 106))
    monkeypatch.setattr(broker_mod, "fetch_current_price", lambda s: 102.0)
    events = paper.poll_exits()
    assert [e.exit_reason for e in events] == [ExitReason.STOP_HIT]
    assert events[0].exit_price == 103.0
    assert events[0].pnl_gross == pytest.approx((100 - 103) * 30)  # loss
    assert "TSLA" not in paper.get_positions()


def test_paper_short_stop_gap_through_fills_at_open(paper, monkeypatch):
    from execution.levels import build_levels

    levels = build_levels("sell", 100.0, 10, [{"price": 103}], [{"price": 90}])
    paper.place_manual_bracket("NVDA", "short", 10, 100.0,
                               [l.to_dict() for l in levels])
    # Gaps open above the stop: buy-to-cover fills at the (worse) open.
    monkeypatch.setattr(broker_mod, "fetch_ohlcv", lambda *a, **k: _bar(107, 106, 108))
    monkeypatch.setattr(broker_mod, "fetch_current_price", lambda s: 107.0)
    events = paper.poll_exits()
    assert events[0].exit_reason == ExitReason.STOP_HIT
    assert events[0].exit_price == 107.0


def test_paper_level_position_survives_restart(paper, settings, monkeypatch):
    _place_long(paper)
    monkeypatch.setattr(broker_mod, "fetch_ohlcv", lambda *a, **k: _bar(99, 96.5, 100))
    monkeypatch.setattr(broker_mod, "fetch_current_price", lambda s: 96.5)
    paper.poll_exits()

    # A new broker instance over the same DATA_DIR restores the ladder state.
    reloaded = PaperBroker(paper._settings)
    pos = reloaded.get_position_detail("AAPL")
    assert pos["quantity"] == 61
    triggered = [l for l in pos["levels"] if l["triggered"]]
    assert len(triggered) == 1 and triggered[0]["price"] == 97.0


def test_paper_modify_stop_refuses_laddered_position(paper):
    _place_long(paper)
    assert paper.modify_stop("AAPL", 99.0) is False


# ------------------------------------------------- risk manager integration


def test_register_position_flags_manual(settings):
    order = build_manual_order(_params())
    rm = RiskManager(settings)
    rm.register_position(order, 100.0)
    pos = rm.get_open_positions()["AAPL"]
    assert pos["manual"] is True
    assert pos["direction"] == "long"
    assert len(pos["levels"]) == 6


def test_exit_manager_skips_manual_positions(settings):
    from execution.exit_manager import ExitManager

    assert ExitManager._is_manual({"manual": True}) is True
    assert ExitManager._is_manual({"strategy": "momentum"}) is False
