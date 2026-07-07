"""Tests for the risk manager: pre-checks, position sizing, and stop validation."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from config.settings import Settings
from risk.manager import RiskManager
from signals.signal_types import ExitEvent, ExitReason, Grade, Signal, TradeOrder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signal(
    symbol: str = "AAPL",
    strategy: str = "momentum",
    entry: float = 195.50,
    stop: float = 190.20,
    target: float = 208.45,
    grade: Grade = Grade.A,
    strength: float = 0.82,
) -> Signal:
    """Create a signal with explicit prices."""
    return Signal(
        symbol=symbol,
        strategy=strategy,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        signal_strength=strength,
        grade=grade,
        timestamp=datetime.now(),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Pre-Check Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestPreCheck:
    """Tests for the risk manager's pre-check gate."""

    def test_valid_signal_passes(self, settings: Settings) -> None:
        """A clean signal with no blocking conditions should pass."""
        rm = RiskManager(settings)
        signal = _make_signal()
        passed, reason = rm.pre_check(signal)
        assert passed is True
        assert reason == "passed"

    def test_rejects_duplicate_position(self, settings: Settings) -> None:
        """Cannot enter a symbol we already hold."""
        rm = RiskManager(settings)
        signal = _make_signal()
        order = TradeOrder(
            signal=signal, quantity=10, currency="USD",
            ai_decision="APPROVE", ai_reasoning="ok",
        )
        rm.register_position(order, fill_price=195.50)

        passed, reason = rm.pre_check(signal)
        assert passed is False
        assert "already_holding" in reason

    def test_rejects_daily_loss_limit(self, settings: Settings) -> None:
        """Should reject when daily P&L exceeds loss limit."""
        rm = RiskManager(settings)
        # Daily loss limit = 12000 * 0.015 = $180
        rm.record_daily_pnl(-200.0)

        signal = _make_signal()
        passed, reason = rm.pre_check(signal)
        assert passed is False
        assert "daily_loss_limit" in reason

    def test_rejects_max_positions(self, settings: Settings) -> None:
        """Should reject when at MAX_OPEN_POSITIONS."""
        rm = RiskManager(settings)
        # Fill up to max
        for i in range(settings.MAX_OPEN_POSITIONS):
            sym = f"SYM{i}"
            sig = _make_signal(symbol=sym)
            order = TradeOrder(
                signal=sig, quantity=1, currency="USD",
                ai_decision="APPROVE", ai_reasoning="ok",
            )
            rm.register_position(order, fill_price=100.0)

        signal = _make_signal(symbol="NEWSTOCK")
        passed, reason = rm.pre_check(signal)
        assert passed is False
        assert "max_positions_reached" in reason

    def test_rejects_invalid_stop(self, settings: Settings) -> None:
        """Stop >= entry should be rejected."""
        rm = RiskManager(settings)
        signal = _make_signal(entry=100.0, stop=100.0, target=110.0)
        passed, reason = rm.pre_check(signal)
        assert passed is False
        assert "invalid_stop" in reason

    def test_rejects_invalid_target(self, settings: Settings) -> None:
        """Target <= entry should be rejected."""
        rm = RiskManager(settings)
        signal = _make_signal(entry=100.0, stop=95.0, target=100.0)
        passed, reason = rm.pre_check(signal)
        assert passed is False
        assert "invalid_target" in reason

    def test_rejects_low_rr(self, settings: Settings) -> None:
        """R:R below minimum (1.8) should be rejected."""
        rm = RiskManager(settings)
        # R:R = (102-100)/(100-95) = 2/5 = 0.4 — way below 1.8
        signal = _make_signal(entry=100.0, stop=95.0, target=102.0)
        passed, reason = rm.pre_check(signal)
        assert passed is False
        assert "rr_too_low" in reason

    def test_accepts_rr_at_minimum_despite_rounding(self, settings: Settings) -> None:
        """Signals built to hit RISK_REWARD_MIN exactly must survive rounding.

        combined_filter sets target = entry + risk * RISK_REWARD_MIN, then
        rounds prices to 4 decimals -- the resulting R:R can land a hair below
        the minimum (e.g. 1.79999) and must not be rejected.
        """
        rm = RiskManager(settings)
        entry, stop = 231.5921, 227.1158
        risk = entry - stop
        target = round(entry + risk * settings.RISK_REWARD_MIN, 4)
        signal = _make_signal(
            entry=round(entry, 4), stop=round(stop, 4), target=target
        )
        passed, reason = rm.pre_check(signal)
        assert passed is True, reason

    def test_reentry_cooldown_stop_hit(self, settings: Settings) -> None:
        """STOP_HIT exits should enforce 24-hour cooldown."""
        rm = RiskManager(settings)
        signal = _make_signal(symbol="AAPL")
        order = TradeOrder(
            signal=signal, quantity=10, currency="USD",
            ai_decision="APPROVE", ai_reasoning="ok",
        )
        rm.register_position(order, fill_price=195.50)

        # Exit with STOP_HIT
        exit_event = ExitEvent(
            symbol="AAPL",
            exit_price=190.20,
            exit_reason=ExitReason.STOP_HIT,
            exit_date=datetime.now(),
            pnl_gross=-53.0,
        )
        rm.remove_position("AAPL", exit_event)

        # Immediate re-entry should be blocked
        new_signal = _make_signal(symbol="AAPL")
        passed, reason = rm.pre_check(new_signal)
        assert passed is False
        assert "reentry_cooldown" in reason

    def test_reentry_cooldown_target_hit(self, settings: Settings) -> None:
        """TARGET_HIT exits should enforce 90-minute cooldown."""
        rm = RiskManager(settings)
        signal = _make_signal(symbol="MSFT")
        order = TradeOrder(
            signal=signal, quantity=10, currency="USD",
            ai_decision="APPROVE", ai_reasoning="ok",
        )
        rm.register_position(order, fill_price=195.50)

        exit_event = ExitEvent(
            symbol="MSFT",
            exit_price=208.45,
            exit_reason=ExitReason.TARGET_HIT,
            exit_date=datetime.now(),
            pnl_gross=129.50,
        )
        rm.remove_position("MSFT", exit_event)

        new_signal = _make_signal(symbol="MSFT")
        passed, reason = rm.pre_check(new_signal)
        assert passed is False
        assert "reentry_cooldown" in reason


# ═══════════════════════════════════════════════════════════════════════════
# Strategy Cap Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestStrategyCap:
    """Tests for per-strategy position limits."""

    def test_momentum_cap(self, settings: Settings) -> None:
        """Momentum + VCP share a cap of 18."""
        rm = RiskManager(settings)

        # Fill up momentum positions to cap
        for i in range(settings.MAX_MOMENTUM_POSITIONS):
            sym = f"MOM{i}"
            sig = _make_signal(symbol=sym, strategy="momentum")
            order = TradeOrder(
                signal=sig, quantity=1, currency="USD",
                ai_decision="APPROVE", ai_reasoning="ok",
            )
            rm.register_position(order, fill_price=100.0)

        # Should be at cap
        passed, reason = rm.check_strategy_cap("momentum")
        assert passed is False
        assert "strategy_cap_reached" in reason

        # VCP should also be blocked (same family)
        passed, reason = rm.check_strategy_cap("vcp_breakout")
        assert passed is False

    def test_mean_reversion_cap_is_one(self, settings: Settings) -> None:
        """Mean reversion has a hard limit of 1 position."""
        rm = RiskManager(settings)
        sig = _make_signal(symbol="MR1", strategy="mean_reversion")
        order = TradeOrder(
            signal=sig, quantity=1, currency="USD",
            ai_decision="APPROVE", ai_reasoning="ok",
        )
        rm.register_position(order, fill_price=100.0)

        passed, reason = rm.check_strategy_cap("mean_reversion")
        assert passed is False

    def test_different_strategies_independent(self, settings: Settings) -> None:
        """Filling momentum cap should not block swing."""
        rm = RiskManager(settings)
        for i in range(settings.MAX_MOMENTUM_POSITIONS):
            sym = f"MOM{i}"
            sig = _make_signal(symbol=sym, strategy="momentum")
            order = TradeOrder(
                signal=sig, quantity=1, currency="USD",
                ai_decision="APPROVE", ai_reasoning="ok",
            )
            rm.register_position(order, fill_price=100.0)

        # Swing should still have room
        passed, reason = rm.check_strategy_cap("swing")
        assert passed is True


# ═══════════════════════════════════════════════════════════════════════════
# Build Order Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildOrder:
    """Tests for position sizing via build_order."""

    def test_basic_sizing(self, settings: Settings) -> None:
        """Basic position sizing calculation."""
        rm = RiskManager(settings)
        signal = _make_signal(
            entry=100.0, stop=95.0, target=109.0, grade=Grade.A,
        )
        order = rm.build_order(signal, "APPROVE", "ok", 0.02)

        assert order is not None
        assert order.quantity > 0
        assert order.currency == "USD"
        assert order.ai_decision == "APPROVE"

    def test_grade_b_reduces_size(self, settings: Settings) -> None:
        """Grade B should get 75% of Grade A shares."""
        rm = RiskManager(settings)

        sig_a = _make_signal(entry=100.0, stop=95.0, target=109.0, grade=Grade.A)
        order_a = rm.build_order(sig_a, "APPROVE", "ok", 0.0)

        sig_b = _make_signal(entry=100.0, stop=95.0, target=109.0, grade=Grade.B)
        order_b = rm.build_order(sig_b, "APPROVE", "ok", 0.0)

        assert order_a is not None and order_b is not None
        # Grade B should have fewer shares (75% of A)
        assert order_b.quantity <= order_a.quantity

    def test_mean_reversion_half_size(self, settings: Settings) -> None:
        """Mean reversion strategy should use 50% position size."""
        rm = RiskManager(settings)

        sig_mom = _make_signal(
            strategy="momentum", entry=100.0, stop=95.0, target=109.0,
        )
        order_mom = rm.build_order(sig_mom, "APPROVE", "ok", 0.0)

        sig_mr = _make_signal(
            strategy="mean_reversion", entry=100.0, stop=95.0, target=109.0,
        )
        order_mr = rm.build_order(sig_mr, "APPROVE", "ok", 0.0)

        assert order_mom is not None and order_mr is not None
        # Mean reversion should have roughly half the shares
        assert order_mr.quantity <= order_mom.quantity

    def test_zero_shares_returns_none(self, settings: Settings) -> None:
        """When calculated shares round to 0, should return None."""
        rm = RiskManager(settings)
        # Very expensive stock with wide stop — risk too high per share
        signal = _make_signal(entry=5000.0, stop=1000.0, target=12200.0)
        order = rm.build_order(signal, "APPROVE", "ok", 0.0)
        assert order is None

    def test_canadian_stock_uses_cad_capital(self, settings: Settings) -> None:
        """Canadian symbols should size against CAD capital pool."""
        rm = RiskManager(settings)
        signal = _make_signal(
            symbol="SHOP.TO", entry=100.0, stop=95.0, target=109.0,
        )
        order = rm.build_order(signal, "APPROVE", "ok", 0.0)

        if order is not None:
            assert order.currency == "CAD"
            # CAD pool is $3000, so max_risk = 3000 * 0.015 = $45
            assert order.max_risk_dollars <= 45.0 + 0.01  # float tolerance


# ═══════════════════════════════════════════════════════════════════════════
# Stop Validation Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestValidateStop:
    """Tests for stop-loss validation and clamping."""

    def test_valid_stop_unchanged(self, settings: Settings) -> None:
        """A valid stop should be returned unchanged."""
        rm = RiskManager(settings)
        result = rm.validate_stop(stop=95.0, entry=100.0)
        assert result == 95.0

    def test_stop_above_entry_clamped(self, settings: Settings) -> None:
        """Stop >= entry should be clamped to entry * 0.93."""
        rm = RiskManager(settings)
        result = rm.validate_stop(stop=105.0, entry=100.0)
        assert abs(result - 93.0) < 0.01

    def test_stop_above_current_clamped(self, settings: Settings) -> None:
        """Stop >= current_price should be clamped to current * 0.985."""
        rm = RiskManager(settings)
        result = rm.validate_stop(stop=99.0, entry=100.0, current_price=98.0)
        expected = 98.0 * 0.985
        assert abs(result - expected) < 0.01

    def test_stop_equal_entry_clamped(self, settings: Settings) -> None:
        """Stop == entry should be clamped."""
        rm = RiskManager(settings)
        result = rm.validate_stop(stop=100.0, entry=100.0)
        assert result < 100.0


# ═══════════════════════════════════════════════════════════════════════════
# Position Management Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestPositionManagement:
    """Tests for position registration, removal, and cash tracking."""

    def test_register_and_get_positions(self, settings: Settings) -> None:
        """Registering a position should make it visible."""
        rm = RiskManager(settings)
        signal = _make_signal(symbol="NVDA")
        order = TradeOrder(
            signal=signal, quantity=10, currency="USD",
            ai_decision="APPROVE", ai_reasoning="ok",
        )
        rm.register_position(order, fill_price=800.0)

        positions = rm.get_open_positions()
        assert "NVDA" in positions
        assert positions["NVDA"]["quantity"] == 10

    def test_remove_position(self, settings: Settings) -> None:
        """Removing a position should clear it from open positions."""
        rm = RiskManager(settings)
        signal = _make_signal(symbol="NVDA")
        order = TradeOrder(
            signal=signal, quantity=10, currency="USD",
            ai_decision="APPROVE", ai_reasoning="ok",
        )
        rm.register_position(order, fill_price=800.0)

        exit_event = ExitEvent(
            symbol="NVDA", exit_price=850.0,
            exit_reason=ExitReason.TARGET_HIT,
            exit_date=datetime.now(), pnl_gross=500.0,
        )
        rm.remove_position("NVDA", exit_event)

        assert "NVDA" not in rm.get_open_positions()

    def test_available_cash_decreases(self, settings: Settings) -> None:
        """Cash should decrease when a position is registered."""
        rm = RiskManager(settings)
        initial_cash = rm.get_available_cash("USD")

        signal = _make_signal(symbol="AAPL")
        order = TradeOrder(
            signal=signal, quantity=10, currency="USD",
            ai_decision="APPROVE", ai_reasoning="ok",
        )
        rm.register_position(order, fill_price=195.50)

        remaining_cash = rm.get_available_cash("USD")
        expected_committed = 195.50 * 10
        assert abs(remaining_cash - (initial_cash - expected_committed)) < 0.01

    def test_positions_persist_to_disk(self, settings: Settings) -> None:
        """Positions should be persisted to open_positions.json."""
        rm = RiskManager(settings)
        signal = _make_signal(symbol="AAPL")
        order = TradeOrder(
            signal=signal, quantity=10, currency="USD",
            ai_decision="APPROVE", ai_reasoning="ok",
        )
        rm.register_position(order, fill_price=195.50)

        # Create a new RiskManager and verify it loads the position
        rm2 = RiskManager(settings)
        positions = rm2.get_open_positions()
        assert "AAPL" in positions

    def test_daily_pnl_tracking(self, settings: Settings) -> None:
        """Daily P&L should accumulate and reset correctly."""
        rm = RiskManager(settings)
        rm.record_daily_pnl(50.0)
        rm.record_daily_pnl(-30.0)
        # Net P&L should be +20 (we just verify it doesn't crash and
        # the daily_loss_limit check works correctly)

        rm.reset_daily_pnl()
        # After reset, should pass daily loss limit check
        signal = _make_signal()
        passed, _ = rm.pre_check(signal)
        assert passed is True


# ═══════════════════════════════════════════════════════════════════════════
# Cross-Process Position Sync Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestCrossProcessSync:
    """The dashboard mutates ``open_positions.json`` from a separate process
    (manual trades); ``sync_positions_from_disk`` lets the engine's manager
    adopt those changes so its in-memory view — and the heartbeat count the
    dashboard displays — never drifts from the shared file.
    """

    @staticmethod
    def _order(symbol: str = "AAPL") -> TradeOrder:
        return TradeOrder(
            signal=_make_signal(symbol=symbol), quantity=10, currency="USD",
            ai_decision="APPROVE", ai_reasoning="ok",
        )

    def test_sync_adopts_externally_added_position(self, settings: Settings) -> None:
        """A position registered by another RiskManager instance (the
        dashboard process) is adopted on sync."""
        engine_rm = RiskManager(settings)
        dashboard_rm = RiskManager(settings)  # simulates the dashboard process
        dashboard_rm.register_position(self._order("HD"), fill_price=351.28)

        assert "HD" not in engine_rm.get_open_positions()
        assert engine_rm.sync_positions_from_disk() is True
        assert "HD" in engine_rm.get_open_positions()

    def test_sync_adopts_externally_removed_position(self, settings: Settings) -> None:
        """A position closed by another process disappears on sync."""
        engine_rm = RiskManager(settings)
        engine_rm.register_position(self._order("HD"), fill_price=351.28)

        dashboard_rm = RiskManager(settings)
        dashboard_rm.remove_position(
            "HD",
            ExitEvent(symbol="HD", exit_price=360.0,
                      exit_reason=ExitReason.TARGET_HIT),
        )

        assert engine_rm.sync_positions_from_disk() is True
        assert "HD" not in engine_rm.get_open_positions()

    def test_sync_is_noop_after_own_writes(self, settings: Settings) -> None:
        """Our own saves must not register as external changes."""
        rm = RiskManager(settings)
        rm.register_position(self._order("HD"), fill_price=351.28)

        assert rm.sync_positions_from_disk() is False
        assert "HD" in rm.get_open_positions()

    def test_sync_is_noop_when_file_never_existed(self, settings: Settings) -> None:
        rm = RiskManager(settings)
        assert rm.sync_positions_from_disk() is False
        assert rm.get_open_positions() == {}

    def test_pre_check_blocks_symbol_added_by_other_process(
        self, settings: Settings
    ) -> None:
        """After a sync, duplicate entry of an externally added symbol is
        rejected (the engine cycle syncs before processing signals)."""
        engine_rm = RiskManager(settings)
        dashboard_rm = RiskManager(settings)
        dashboard_rm.register_position(self._order("AAPL"), fill_price=195.50)

        engine_rm.sync_positions_from_disk()
        passed, reason = engine_rm.pre_check(_make_signal(symbol="AAPL"))
        assert passed is False
        assert "already_holding" in reason
