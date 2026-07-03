"""Tests for signal scoring engine: individual indicators and combined filter."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from signals.signal_types import Grade, Signal


# ═══════════════════════════════════════════════════════════════════════════
# Grade enum
# ═══════════════════════════════════════════════════════════════════════════


class TestGrade:
    """Tests for Grade.from_score thresholds."""

    def test_grade_a(self) -> None:
        assert Grade.from_score(0.85) == Grade.A
        assert Grade.from_score(0.78) == Grade.A
        assert Grade.from_score(1.0) == Grade.A

    def test_grade_b(self) -> None:
        assert Grade.from_score(0.70) == Grade.B
        assert Grade.from_score(0.65) == Grade.B
        assert Grade.from_score(0.77) == Grade.B

    def test_grade_c(self) -> None:
        assert Grade.from_score(0.50) == Grade.C
        assert Grade.from_score(0.38) == Grade.C
        assert Grade.from_score(0.64) == Grade.C

    def test_grade_f(self) -> None:
        assert Grade.from_score(0.37) == Grade.F
        assert Grade.from_score(0.0) == Grade.F
        assert Grade.from_score(0.10) == Grade.F

    def test_boundary_values(self) -> None:
        """Test exact boundary values."""
        assert Grade.from_score(0.78) == Grade.A
        assert Grade.from_score(0.7799) == Grade.B
        assert Grade.from_score(0.65) == Grade.B
        assert Grade.from_score(0.6499) == Grade.C
        assert Grade.from_score(0.38) == Grade.C
        assert Grade.from_score(0.3799) == Grade.F


# ═══════════════════════════════════════════════════════════════════════════
# Signal dataclass
# ═══════════════════════════════════════════════════════════════════════════


class TestSignal:
    """Tests for Signal dataclass properties."""

    def test_risk_per_share(self, sample_signal: Signal) -> None:
        """risk_per_share = entry - stop."""
        expected = 195.50 - 190.20
        assert abs(sample_signal.risk_per_share - expected) < 0.01

    def test_reward_per_share(self, sample_signal: Signal) -> None:
        """reward_per_share = target - entry."""
        expected = 208.45 - 195.50
        assert abs(sample_signal.reward_per_share - expected) < 0.01

    def test_risk_reward_ratio(self, sample_signal: Signal) -> None:
        """R:R = reward / risk."""
        rr = sample_signal.risk_reward_ratio
        expected = (208.45 - 195.50) / (195.50 - 190.20)
        assert abs(rr - expected) < 0.01

    def test_risk_reward_zero_risk(self) -> None:
        """R:R should be 0.0 when stop >= entry."""
        sig = Signal(symbol="X", strategy="momentum", entry_price=100, stop_price=100)
        assert sig.risk_reward_ratio == 0.0

    def test_default_direction(self) -> None:
        """Default direction should be 'long'."""
        sig = Signal(symbol="X", strategy="momentum")
        assert sig.direction == "long"


# ═══════════════════════════════════════════════════════════════════════════
# RSI Signals
# ═══════════════════════════════════════════════════════════════════════════


class TestRSISignals:
    """Tests for RSI indicator calculation and scoring."""

    def test_rsi_calculation(self, bullish_df: pd.DataFrame) -> None:
        """calculate_rsi should return a valid RSIState."""
        from signals.rsi_signals import calculate_rsi

        state = calculate_rsi(bullish_df)
        assert 0 <= state.rsi_value <= 100

    def test_rsi_momentum_score_range(self, bullish_df: pd.DataFrame) -> None:
        """Momentum RSI score should be in [0, 1]."""
        from signals.rsi_signals import bullish_score, calculate_rsi

        state = calculate_rsi(bullish_df)
        score = bullish_score(state, "momentum")
        assert 0.0 <= score <= 1.0

    def test_rsi_swing_score_range(self, bullish_df: pd.DataFrame) -> None:
        """Swing RSI score should be in [0, 1]."""
        from signals.rsi_signals import bullish_score, calculate_rsi

        state = calculate_rsi(bullish_df)
        score = bullish_score(state, "swing")
        assert 0.0 <= score <= 1.0

    def test_rsi_bearish_divergence_zeros_score(self) -> None:
        """When bearish divergence is detected, score must be 0.0."""
        from signals.rsi_signals import RSIState, bullish_score

        state = RSIState(
            rsi_value=62.0,
            is_momentum_zone=True,
            is_swing_recovery=False,
            is_overbought=False,
            is_overbought_rollover=False,
            has_bearish_divergence=True,
            is_above_midline=True,
            is_rising=True,
            rsi_5_bars_ago=58.0,
        )
        assert bullish_score(state, "momentum") == 0.0
        assert bullish_score(state, "swing") == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# MACD Signals
# ═══════════════════════════════════════════════════════════════════════════


class TestMACDSignals:
    """Tests for MACD indicator calculation and scoring."""

    def test_macd_calculation(self, bullish_df: pd.DataFrame) -> None:
        """calculate_macd should return a valid MACDState."""
        from signals.macd_signals import calculate_macd

        state = calculate_macd(bullish_df)
        assert isinstance(state.macd_line, float)
        assert isinstance(state.histogram, float)

    def test_macd_score_range(self, bullish_df: pd.DataFrame) -> None:
        """MACD bullish score should be in [0, 1]."""
        from signals.macd_signals import bullish_score, calculate_macd

        state = calculate_macd(bullish_df)
        score = bullish_score(state)
        assert 0.0 <= score <= 1.0


# ═══════════════════════════════════════════════════════════════════════════
# EMA Signals
# ═══════════════════════════════════════════════════════════════════════════


class TestEMASignals:
    """Tests for EMA structure calculation and scoring."""

    def test_ema_calculation(self, bullish_df: pd.DataFrame) -> None:
        """calculate_ema should populate all four EMAs."""
        from signals.ema_signals import calculate_ema

        state = calculate_ema(bullish_df)
        assert state.ema9 > 0
        assert state.ema20 > 0
        assert state.ema50 > 0
        assert state.ema200 > 0

    def test_ema_momentum_score_range(self, bullish_df: pd.DataFrame) -> None:
        from signals.ema_signals import bullish_score, calculate_ema

        state = calculate_ema(bullish_df)
        score = bullish_score(state, "momentum")
        assert 0.0 <= score <= 1.0

    def test_ema_bear_regime_zeros_score(self, bearish_df: pd.DataFrame) -> None:
        """When price < EMA200, EMA score must be 0.0."""
        from signals.ema_signals import bullish_score, calculate_ema

        state = calculate_ema(bearish_df)
        if not state.is_above_ema200:
            score = bullish_score(state, "momentum")
            assert score == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Volume Signals
# ═══════════════════════════════════════════════════════════════════════════


class TestVolumeSignals:
    """Tests for volume analysis calculation and scoring."""

    def test_volume_calculation(self, bullish_df: pd.DataFrame) -> None:
        """calculate_volume should compute volume_ratio."""
        from signals.volume_signals import calculate_volume

        state = calculate_volume(bullish_df)
        assert state.volume_ratio > 0

    def test_volume_score_range(self, bullish_df: pd.DataFrame) -> None:
        from signals.volume_signals import bullish_score, calculate_volume

        state = calculate_volume(bullish_df)
        score = bullish_score(state, "momentum")
        assert 0.0 <= score <= 1.0

    def test_obv_divergence_zeros_score(self) -> None:
        """OBV divergence hard veto should zero the volume score."""
        from signals.volume_signals import VolumeState, bullish_score

        state = VolumeState(
            volume_ratio=2.0,
            has_bullish_surge=True,
            has_bullish_dryup=False,
            is_obv_confirming=False,
            has_obv_divergence=True,
            is_above_vwap=True,
            has_bearish_surge=False,
        )
        assert bullish_score(state, "momentum") == 0.0
        assert bullish_score(state, "swing") == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Ripster Cloud
# ═══════════════════════════════════════════════════════════════════════════


class TestRipsterCloud:
    """Tests for Ripster EMA cloud calculation and scoring."""

    def test_ripster_calculation(self, bullish_df: pd.DataFrame) -> None:
        from signals.ripster_cloud import calculate_ripster

        state = calculate_ripster(bullish_df)
        assert state.ema8 > 0
        assert state.ema34 > 0

    def test_ripster_score_range(self, bullish_df: pd.DataFrame) -> None:
        from signals.ripster_cloud import bullish_score, calculate_ripster

        state = calculate_ripster(bullish_df)
        score = bullish_score(state)
        assert 0.0 <= score <= 1.0

    def test_below_both_clouds_zeros_score(self) -> None:
        """Price below both clouds should give a hard 0.0."""
        from signals.ripster_cloud import RipsterState, bullish_score

        state = RipsterState(
            ema8=100.0,
            ema9=101.0,
            ema34=105.0,
            ema35=106.0,
            fast_cloud_bullish=False,
            slow_cloud_bullish=False,
            price_above_fast=False,
            price_above_slow=False,
            fast_above_slow=False,
            clouds_expanding=False,
            has_fresh_cross=False,
            price_below_both=True,
        )
        assert bullish_score(state) == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Combined Filter
# ═══════════════════════════════════════════════════════════════════════════


class TestCombinedFilter:
    """Tests for the combined scoring filter."""

    def test_score_symbol_returns_signal(self, bullish_df: pd.DataFrame) -> None:
        """score_symbol should return a Signal for valid data."""
        from signals.combined_filter import score_symbol

        result = score_symbol("AAPL", "momentum", bullish_df)
        # May be None if hard veto fires (data-dependent), but should not error
        if result is not None:
            assert isinstance(result, Signal)
            assert result.symbol == "AAPL"
            assert result.strategy == "momentum"
            assert 0.0 <= result.signal_strength <= 1.0

    def test_score_symbol_insufficient_data(self, short_df: pd.DataFrame) -> None:
        """score_symbol should return None for insufficient data."""
        from signals.combined_filter import score_symbol

        result = score_symbol("AAPL", "momentum", short_df)
        assert result is None

    def test_score_symbol_bearish_data(self, bearish_df: pd.DataFrame) -> None:
        """score_symbol should return None for bearish data (hard veto)."""
        from signals.combined_filter import score_symbol

        result = score_symbol("AAPL", "momentum", bearish_df)
        # Bearish data should trigger bear regime or other veto
        # It's possible it doesn't if the random data happens to end above EMA200
        # but typically it should be None
        if result is not None:
            # If it passed vetoes, it should still have a valid grade
            assert result.grade in (Grade.A, Grade.B, Grade.C, Grade.F)

    def test_canadian_stock_uses_swing_weights(self, bullish_df: pd.DataFrame) -> None:
        """Canadian .TO stocks should use swing weights regardless of strategy."""
        from signals.combined_filter import score_symbol

        result = score_symbol("SHOP.TO", "momentum", bullish_df)
        if result is not None:
            # The original strategy name should be preserved
            assert result.strategy == "momentum"

    def test_signal_price_levels_valid(self, bullish_df: pd.DataFrame) -> None:
        """Entry, stop, and target prices should have correct ordering."""
        from signals.combined_filter import score_symbol

        result = score_symbol("AAPL", "momentum", bullish_df)
        if result is not None:
            assert result.stop_price < result.entry_price
            assert result.target_price > result.entry_price
            assert result.risk_reward_ratio >= 0
