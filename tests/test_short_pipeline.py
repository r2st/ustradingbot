"""Core-pipeline direction-awareness tests for the short-selling module.

Covers the changes that let ``direction="short"`` signals flow through the
existing engine pipeline: Signal maths, RiskManager gates and sizing, the
paper broker's side-aware bracket/exits/stop-ratchet, and the exit manager's
short handling.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import pytest

from config.settings import Settings
from execution.broker import PaperBroker
from execution.exit_manager import ExitManager
from journal.trade_logger import TradeLogger
from risk.manager import RiskManager
from signals.signal_types import ExitReason, Grade, Signal, TradeOrder


def _short_signal(
    symbol: str = "SHRT",
    entry: float = 100.0,
    stop: float = 104.0,
    target: float = 92.0,
    strategy: str = "short_gap_fail",
    strength: float = 0.8,
) -> Signal:
    return Signal(
        symbol=symbol,
        strategy=strategy,
        direction="short",
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        signal_strength=strength,
        grade=Grade.from_score(strength),
        timestamp=datetime.now(),
    )


# ---------------------------------------------------------------------------
# Signal maths
# ---------------------------------------------------------------------------


class TestShortSignalMaths:
    def test_direction_aware_risk_reward(self):
        sig = _short_signal()
        assert sig.is_short
        assert sig.risk_per_share == pytest.approx(4.0)
        assert sig.reward_per_share == pytest.approx(8.0)
        assert sig.risk_reward_ratio == pytest.approx(2.0)

    def test_long_maths_unchanged(self, sample_signal):
        assert not sample_signal.is_short
        assert sample_signal.risk_per_share == pytest.approx(
            sample_signal.entry_price - sample_signal.stop_price
        )


# ---------------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------------


class TestRiskManagerShort:
    def test_pre_check_accepts_valid_short(self, settings):
        rm = RiskManager(settings)
        ok, reason = rm.pre_check(_short_signal())
        assert ok, reason

    def test_pre_check_rejects_stop_below_entry(self, settings):
        rm = RiskManager(settings)
        ok, reason = rm.pre_check(_short_signal(stop=99.0, target=92.0))
        assert not ok and reason.startswith("invalid_stop")

    def test_pre_check_rejects_target_above_entry(self, settings):
        rm = RiskManager(settings)
        ok, reason = rm.pre_check(_short_signal(target=101.0))
        assert not ok and reason.startswith("invalid_target")

    def test_pre_check_rejects_low_rr_short(self, settings):
        # risk 4, reward 4 -> R:R 1.0 < 1.8 minimum.
        rm = RiskManager(settings)
        ok, reason = rm.pre_check(_short_signal(target=96.0))
        assert not ok and reason.startswith("rr_too_low")

    def test_short_family_cap(self, settings, monkeypatch):
        monkeypatch.setenv("SHORT_MAX_POSITIONS", "1")
        from short_strategies.common.config import get_short_config

        get_short_config.cache_clear()
        try:
            rm = RiskManager(settings)
            ok, _ = rm.check_strategy_cap("short_gap_fail")
            assert ok
            order = TradeOrder(signal=_short_signal("AAA"), quantity=10,
                               risk_amount=40.0, currency="USD")
            rm.register_position(order, fill_price=100.0)
            ok, reason = rm.check_strategy_cap("short_bear_flag")
            assert not ok and reason.startswith("strategy_cap_reached:short")
        finally:
            get_short_config.cache_clear()

    def test_build_order_sizes_short_with_modifier(self, settings):
        rm = RiskManager(settings)
        sig = _short_signal()
        order = rm.build_order(sig, "APPROVE", "ok", 0.0)
        assert order is not None
        # Budget = 9000 (USD pool) * 1.5% * 0.65 modifier = 87.75 -> 21 shares
        # at $4 risk/share, then capped by the 10%-of-pool notional limit
        # (9000 * 0.10 / $100 = 9 shares).
        assert order.max_risk_dollars == pytest.approx(9000 * 0.015 * 0.65)
        assert order.quantity == 9
        assert order.risk_amount == pytest.approx(order.quantity * 4.0)

    def test_position_registers_short_direction(self, settings):
        rm = RiskManager(settings)
        order = TradeOrder(signal=_short_signal(), quantity=10,
                           risk_amount=40.0, currency="USD")
        rm.register_position(order, fill_price=99.9)
        pos = rm.get_open_positions()["SHRT"]
        assert pos["direction"] == "short"


# ---------------------------------------------------------------------------
# PaperBroker
# ---------------------------------------------------------------------------


def _paper_broker(tmp_path: Path) -> PaperBroker:
    settings = Settings(DATA_DIR=tmp_path / "broker_data")
    broker = PaperBroker(settings)
    broker.connect()
    return broker


class TestPaperBrokerShort:
    def test_short_entry_fills_below_with_slippage(self, tmp_path):
        broker = _paper_broker(tmp_path)
        res = broker.place_bracket_order(
            "SHRT", 10, 100.0, 104.0, 92.0, side="short"
        )
        assert res.accepted
        assert res.fill_price < 100.0  # adverse slippage is DOWN for a short
        detail = broker.get_position_detail("SHRT")
        assert detail["side"] == "short"

    def test_invalid_side_rejected(self, tmp_path):
        broker = _paper_broker(tmp_path)
        res = broker.place_bracket_order("SHRT", 10, 100.0, 104.0, 92.0,
                                         side="sideways")
        assert not res.accepted

    def test_short_stop_hit_on_rally(self, tmp_path, monkeypatch):
        broker = _paper_broker(tmp_path)
        broker.place_bracket_order("SHRT", 10, 100.0, 104.0, 92.0, side="short")
        # Bar rallies through the buy-stop: open 101, low 100, high 106.
        monkeypatch.setattr(broker, "_latest_bar", lambda s: (101.0, 100.0, 106.0))
        events = broker.poll_exits()
        assert len(events) == 1
        ev = events[0]
        assert ev.exit_reason == ExitReason.STOP_HIT
        assert ev.exit_price >= 104.0  # fills at/above the stop (adverse)
        assert ev.pnl_gross < 0

    def test_short_target_hit_on_decline(self, tmp_path, monkeypatch):
        broker = _paper_broker(tmp_path)
        broker.place_bracket_order("SHRT", 10, 100.0, 104.0, 92.0, side="short")
        monkeypatch.setattr(broker, "_latest_bar", lambda s: (95.0, 91.0, 96.0))
        events = broker.poll_exits()
        assert len(events) == 1
        ev = events[0]
        assert ev.exit_reason == ExitReason.TARGET_HIT
        assert ev.pnl_gross > 0

    def test_short_gap_through_stop_fills_at_open(self, tmp_path, monkeypatch):
        broker = _paper_broker(tmp_path)
        broker.place_bracket_order("SHRT", 10, 100.0, 104.0, 92.0, side="short")
        # Gaps open ABOVE the buy-stop: fills at the worse open.
        monkeypatch.setattr(broker, "_latest_bar", lambda s: (108.0, 107.0, 110.0))
        events = broker.poll_exits()
        assert events[0].exit_price == pytest.approx(108.0)

    def test_short_partial_take_below_entry(self, tmp_path, monkeypatch):
        broker = _paper_broker(tmp_path)
        broker.place_bracket_order(
            "SHRT", 10, 100.0, 104.0, 92.0, side="short",
            partial_take_pct=0.5, partial_take_target_r=1.0,
        )
        detail = broker.get_position_detail("SHRT")
        assert 0 < detail["partial_take_price"] < 100.0
        # Bar dips to the partial-take level but not the full target.
        pt = detail["partial_take_price"]
        monkeypatch.setattr(broker, "_latest_bar",
                            lambda s: (pt + 1.0, pt - 0.2, pt + 2.0))
        events = broker.poll_exits()
        assert len(events) == 1
        assert events[0].exit_reason == ExitReason.PARTIAL_TAKE
        assert events[0].pnl_gross > 0
        assert broker.get_position_detail("SHRT")["quantity"] == 5

    def test_modify_stop_ratchets_down_only_for_short(self, tmp_path):
        broker = _paper_broker(tmp_path)
        broker.place_bracket_order("SHRT", 10, 100.0, 104.0, 92.0, side="short")
        assert broker.modify_stop("SHRT", 105.0) is False  # looser: refused
        assert broker.modify_stop("SHRT", 102.0) is True   # tighter: ok
        assert broker.get_position_detail("SHRT")["stop_price"] == 102.0

    def test_long_bracket_unchanged(self, tmp_path):
        broker = _paper_broker(tmp_path)
        res = broker.place_bracket_order("LONG", 10, 100.0, 96.0, 108.0)
        assert res.accepted
        assert res.fill_price > 100.0
        assert broker.get_position_detail("LONG")["side"] == "long"

    def test_short_position_survives_reload(self, tmp_path):
        broker = _paper_broker(tmp_path)
        broker.place_bracket_order("SHRT", 10, 100.0, 104.0, 92.0, side="short")
        settings = Settings(DATA_DIR=tmp_path / "broker_data")
        broker2 = PaperBroker(settings)
        broker2.connect()
        assert broker2.get_position_detail("SHRT")["side"] == "short"


# ---------------------------------------------------------------------------
# ExitManager
# ---------------------------------------------------------------------------


class _StubBroker:
    """Minimal broker double for exit-manager sweeps."""

    def __init__(self):
        self.force_closed: list = []
        self.modified: list = []

    def poll_exits(self):
        return []

    def force_close(self, symbol, reason):
        self.force_closed.append((symbol, reason))
        return None  # exit manager synthesises the event from risk state

    def modify_stop(self, symbol, new_stop):
        self.modified.append((symbol, new_stop))
        return True


def _register_short(
    rm: RiskManager,
    symbol: str = "SHRT",
    entry: float = 100.0,
    days_ago: int = 0,
) -> None:
    order = TradeOrder(signal=_short_signal(symbol, entry=entry), quantity=10,
                       risk_amount=40.0, currency="USD")
    rm.register_position(order, fill_price=entry)
    if days_ago:
        from datetime import timedelta

        pos = rm._positions[symbol]  # test-only reach-in
        pos["entry_time"] = (
            datetime.now() - timedelta(days=days_ago)
        ).isoformat()
        rm._save_positions()


class TestExitManagerShort:
    def _manager(self, settings, broker) -> tuple[ExitManager, RiskManager]:
        rm = RiskManager(settings)
        journal = TradeLogger(str(settings.DATA_DIR))
        return ExitManager(settings, broker, rm, journal), rm

    def test_health_and_dynamic_sweeps_skip_shorts(self, settings, monkeypatch):
        broker = _StubBroker()
        manager, rm = self._manager(settings, broker)
        _register_short(rm)

        fetches: list = []

        def spy_fetch(symbol, period=None, **kwargs):
            fetches.append(symbol)
            return None

        monkeypatch.setattr("execution.exit_manager.fetch_ohlcv", spy_fetch)
        assert manager._check_position_health() == 0
        assert manager._check_dynamic_stops() == 0
        # Neither sweep should even have fetched data for the short.
        assert fetches == []

    def test_time_exit_profitable_short_trails_stop_down(
        self, settings, monkeypatch
    ):
        broker = _StubBroker()
        manager, rm = self._manager(settings, broker)
        # Held past max hold; price fell 20% -> strong short winner.
        _register_short(rm, days_ago=25)
        monkeypatch.setattr(
            "execution.exit_manager.fetch_current_price", lambda s: 80.0
        )
        manager._check_time_based_exits()
        # Winner is kept: the stop trails DOWN to lock in half the gain.
        assert broker.force_closed == []
        assert broker.modified == [("SHRT", 90.0)]

    def test_time_exit_losing_short_closed_with_direction_aware_pnl(
        self, settings, monkeypatch
    ):
        broker = _StubBroker()
        manager, rm = self._manager(settings, broker)
        # Held past max hold; price ROSE 3% -> losing short.
        _register_short(rm, days_ago=25)
        monkeypatch.setattr(
            "execution.exit_manager.fetch_current_price", lambda s: 103.0
        )
        manager._check_time_based_exits()
        assert broker.force_closed == [("SHRT", ExitReason.TIME_EXIT_LOSS)]
        # Synthesised event P&L must be direction-aware: (100-103)*10 = -30.
        assert rm.daily_pnl == pytest.approx(-30.0)


# ---------------------------------------------------------------------------
# Engine wiring
# ---------------------------------------------------------------------------


class TestEngineShortScanWiring:
    class _FakeSelection:
        def __init__(self, allowed):
            self._allowed = allowed

        def allowed_strategies(self):
            return self._allowed

        def effective_min_grade(self, default):
            return default

    class _FakeEngineSelf:
        def __init__(self, rm):
            self.risk_manager = rm

    def test_long_only_whitelist_disables_short_scan(self, settings):
        from engine import TradingEngine

        fake = self._FakeEngineSelf(RiskManager(settings))
        selection = self._FakeSelection(["momentum", "swing"])
        out = TradingEngine._run_short_scan(fake, ["AAA"], selection)
        assert out == []

    def test_disabled_module_returns_empty(self, settings, monkeypatch):
        from engine import TradingEngine
        from short_strategies.common.config import get_short_config

        monkeypatch.setenv("SHORT_STRATEGIES_ENABLED", "false")
        get_short_config.cache_clear()
        try:
            fake = self._FakeEngineSelf(RiskManager(settings))
            selection = self._FakeSelection(None)
            out = TradingEngine._run_short_scan(fake, ["AAA"], selection)
            assert out == []
        finally:
            get_short_config.cache_clear()


# ---------------------------------------------------------------------------
# AI prompt direction-awareness
# ---------------------------------------------------------------------------


class TestAIPrompt:
    def test_short_prompt_mentions_short_guidance(self):
        from ai.analyst import _build_user_prompt

        prompt = _build_user_prompt(_short_signal())
        assert "SHORT SALE" in prompt
        assert "short trade" in prompt

    def test_long_prompt_unchanged(self, sample_signal):
        from ai.analyst import _build_user_prompt

        prompt = _build_user_prompt(sample_signal)
        assert "long trade" in prompt
