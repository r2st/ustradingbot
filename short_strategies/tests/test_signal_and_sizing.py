"""Tests for ShortSignal conversion and the ATR sizing maths."""

from __future__ import annotations

import pytest

from short_strategies.common.config import SharedFilterConfig
from short_strategies.common.signal import ShortSignal
from short_strategies.risk.sizing import short_stop_target, size_short_position
from signals.signal_types import Grade


def _sig(strength: float = 0.8) -> ShortSignal:
    return ShortSignal(
        strategy_id="short_gap_fail",
        symbol="XYZ",
        signal_strength=strength,
        trigger_price=100.0,
        stop_price=104.0,
        target_price=92.0,
        filters_passed=["borrow_locate"],
        metadata={"gap_pct": 0.05},
    )


class TestShortSignal:
    def test_risk_and_reward_per_share(self):
        sig = _sig()
        assert sig.risk_per_share == pytest.approx(4.0)
        assert sig.reward_per_share == pytest.approx(8.0)

    def test_price_validity(self):
        assert _sig().is_price_valid()
        bad = _sig()
        bad.stop_price = 99.0  # stop below entry is invalid for a short
        assert not bad.is_price_valid()

    def test_to_core_signal_direction_and_grade(self):
        core = _sig(0.8).to_core_signal()
        assert core.direction == "short"
        assert core.is_short
        assert core.grade == Grade.A
        assert core.strategy == "short_gap_fail"
        assert core.entry_price == 100.0
        assert core.raw_data["side"] == "SHORT"
        assert core.raw_data["filters_passed"] == ["borrow_locate"]
        assert core.raw_data["metadata"]["gap_pct"] == 0.05

    def test_core_signal_risk_reward_is_direction_aware(self):
        core = _sig().to_core_signal()
        assert core.risk_per_share == pytest.approx(4.0)
        assert core.reward_per_share == pytest.approx(8.0)
        assert core.risk_reward_ratio == pytest.approx(2.0)


class TestShortStopTarget:
    def test_atr_stop_above_entry(self):
        cfg = SharedFilterConfig(stop_atr_mult=1.5, target_rr=2.0)
        stop, target = short_stop_target(100.0, 2.0, cfg)
        assert stop == pytest.approx(103.0)
        assert target == pytest.approx(94.0)

    def test_structural_stop_preferred_when_tighter(self):
        cfg = SharedFilterConfig(stop_atr_mult=1.5, target_rr=2.0)
        stop, _ = short_stop_target(100.0, 2.0, cfg, structural_stop=101.5)
        assert stop == pytest.approx(101.5)

    def test_structural_stop_capped_at_max_atr(self):
        cfg = SharedFilterConfig(stop_atr_mult=1.5, target_rr=2.0,
                                 max_stop_atr_mult=2.5)
        stop, _ = short_stop_target(100.0, 2.0, cfg, structural_stop=120.0)
        assert stop == pytest.approx(105.0)  # 100 + 2.5 * 2

    def test_structural_stop_below_entry_ignored(self):
        cfg = SharedFilterConfig(stop_atr_mult=1.5, target_rr=2.0)
        stop, _ = short_stop_target(100.0, 2.0, cfg, structural_stop=98.0)
        assert stop == pytest.approx(103.0)

    def test_target_never_non_positive(self):
        cfg = SharedFilterConfig(stop_atr_mult=1.5, target_rr=10.0)
        _, target = short_stop_target(1.0, 5.0, cfg)
        assert target > 0


class TestSizeShortPosition:
    def test_spec_risk_band(self):
        # 1% of $10,000 = $100 risk; $2 risk/share -> 50 shares.
        assert size_short_position(10_000, 0.01, 100.0, 102.0) == 50

    def test_zero_on_degenerate_inputs(self):
        assert size_short_position(10_000, 0.01, 100.0, 99.0) == 0
        assert size_short_position(0, 0.01, 100.0, 102.0) == 0
        assert size_short_position(10_000, 0.0, 100.0, 102.0) == 0
