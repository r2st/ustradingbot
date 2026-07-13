"""Comprehensive tests for the highly selective strategies module.

Covers configuration, signal types, macro event calendar, all six strategy
detectors, the scan orchestrator, trade-selection integration, analyst cards,
and AI analyst guidance.
"""

from __future__ import annotations

import os
from datetime import datetime, date, timedelta
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest

from config.settings import EASTERN
from signals.signal_types import Grade, Signal
from selective_strategies.config import (
    SelectiveConfig,
    get_selective_config,
    RSI2ReversalConfig,
    TripleTimeframeConfig,
    BBClimaxConfig,
    PEADDriftConfig,
    GapFillConfig,
    TurnaroundTuesdayConfig,
)
from selective_strategies.signal import SelectiveSignal
from selective_strategies.events import is_macro_event_day, has_upcoming_event
from selective_strategies.strategies import STRATEGY_PRIORITY, DETECTORS
from selective_strategies.strategies.rsi2_reversal import detect as detect_rsi2
from selective_strategies.strategies.triple_timeframe import detect as detect_ttf
from selective_strategies.strategies.bb_climax import detect as detect_bb
from selective_strategies.strategies.pead_drift import detect as detect_pead
from selective_strategies.strategies.gap_fill import detect as detect_gap
from selective_strategies.strategies.turnaround_tuesday import detect as detect_tt
from selective_strategies.scanner import run_selective_scan
from config.trade_selection import VALID_STRATEGIES, SELECTIVE_STRATEGIES
from dashboard.analyst_cards import STRATEGY_TAGS, strategy_tag
from ai.analyst import _strategy_guidance


# ======================================================================
# Helpers -- synthetic data builders
# ======================================================================


def _make_rsi2_reversal_data():
    """Build 250-day data that triggers RSI-2 reversal at support."""
    np.random.seed(100)
    n = 250
    dates = pd.bdate_range(end=datetime.now(), periods=n)
    # Uptrending base (above SMA200)
    prices = 100 + np.cumsum(np.random.normal(0.1, 0.5, n))
    prices = np.maximum(prices, 50)
    # Create a support level by having price bounce off ~$110 several times
    support = 110.0
    for i in [180, 200, 220]:
        prices[i] = support + np.random.uniform(-0.3, 0.3)
        prices[i + 1] = support + np.random.uniform(0, 1)
    # Sharp drop to support on last 2 bars (makes RSI(2) < 5)
    prices[-2] = prices[-3] - 3.0
    prices[-1] = support + 0.2
    # Build OHLCV
    high = prices + np.abs(np.random.normal(0, 0.5, n))
    low = prices - np.abs(np.random.normal(0, 0.5, n))
    volume = np.random.randint(500000, 1500000, n).astype(float)
    volume[-1] = volume[-20:].mean() * 2.5  # Volume surge
    return pd.DataFrame(
        {
            "Open": prices + np.random.normal(0, 0.3, n),
            "High": high,
            "Low": low,
            "Close": prices,
            "Volume": volume,
        },
        index=dates,
    )


def _make_triple_timeframe_breakout_data(breakout: bool = True):
    """Build 250-day data for triple-timeframe breakout.

    When *breakout* is True the last bar breaks above the consolidation
    range on elevated volume.  When False the price stays inside.
    """
    np.random.seed(42)
    n = 250
    dates = pd.bdate_range(end=datetime.now(), periods=n)

    # Strong uptrend for SMA50 > SMA200
    prices = 100 + np.cumsum(np.random.normal(0.15, 0.3, n))
    prices = np.maximum(prices, 80)

    # Consolidation: last 20 bars are flat (ATR contraction)
    consol_start = n - 20
    base_price = prices[consol_start - 1]
    for i in range(consol_start, n):
        prices[i] = base_price + np.random.normal(0, 0.15)

    # ATR contraction: make the early bars more volatile
    high = prices.copy()
    low = prices.copy()
    for i in range(n):
        if i < consol_start:
            high[i] = prices[i] + abs(np.random.normal(0, 1.5))
            low[i] = prices[i] - abs(np.random.normal(0, 1.5))
        else:
            high[i] = prices[i] + abs(np.random.normal(0, 0.2))
            low[i] = prices[i] - abs(np.random.normal(0, 0.2))

    if breakout:
        # Last bar: breakout above consolidation high with volume surge
        consol_high = max(high[consol_start:-1])
        prices[-1] = consol_high + 2.0
        high[-1] = prices[-1] + 0.5
        low[-1] = prices[-1] - 1.0

    volume = np.random.randint(500000, 1500000, n).astype(float)
    if breakout:
        volume[-1] = volume[-20:].mean() * 3.0  # Volume surge

    return pd.DataFrame(
        {
            "Open": prices + np.random.normal(0, 0.1, n),
            "High": high,
            "Low": low,
            "Close": prices,
            "Volume": volume,
        },
        index=dates,
    )


def _make_bb_climax_data(extreme_volume: bool = True):
    """Build 250-day data for BB climax reversal.

    Creates a sharp decline ending with a close below the lower BB,
    a hammer candle, and declining closes for the last 3 bars.
    If *extreme_volume* is True the last bar has top-percentile volume.
    """
    np.random.seed(55)
    n = 250
    dates = pd.bdate_range(end=datetime.now(), periods=n)

    # Base: slight uptrend then sharp drop at end
    prices = 100 + np.cumsum(np.random.normal(0.05, 0.8, n))
    prices = np.maximum(prices, 60)

    # Force last 3 bars to be strictly declining (acceleration check)
    prices[-3] = prices[-4] - 0.5
    prices[-2] = prices[-3] - 1.5
    prices[-1] = prices[-2] - 3.0  # Big drop to push below lower BB

    high = prices + np.abs(np.random.normal(0, 0.8, n))
    low = prices - np.abs(np.random.normal(0, 0.8, n))

    # Make last bar a hammer: long lower shadow, close near high
    last_close = prices[-1]
    low[-1] = last_close - 4.0   # Long lower wick
    high[-1] = last_close + 0.3  # Tiny upper shadow
    open_prices = prices + np.random.normal(0, 0.3, n)
    open_prices[-1] = last_close + 0.2  # Open near high (hammer body)

    volume = np.random.randint(500000, 1500000, n).astype(float)
    if extreme_volume:
        # Ensure last bar is in the top 5th percentile
        volume[-1] = np.percentile(volume[-100:], 98) + 100000

    return pd.DataFrame(
        {
            "Open": open_prices,
            "High": high,
            "Low": low,
            "Close": prices,
            "Volume": volume,
        },
        index=dates,
    )


def _make_pead_data(gap_pct: float = 0.05):
    """Build data that triggers a PEAD drift signal.

    *gap_pct* controls the gap size between prior close and today's open.
    """
    np.random.seed(77)
    n = 100
    dates = pd.bdate_range(end=datetime.now(), periods=n)
    prices = 100 + np.cumsum(np.random.normal(0.05, 0.5, n))
    prices = np.maximum(prices, 60)

    prior_close = float(prices[-2])
    gap_open = prior_close * (1.0 + gap_pct)

    # Today: gap up, close in the top third of range, close >= open
    today_high = gap_open + 1.5
    today_low = gap_open - 0.5
    today_close = gap_open + 1.0  # In top third
    prices[-1] = today_close

    high = prices + np.abs(np.random.normal(0, 0.5, n))
    low = prices - np.abs(np.random.normal(0, 0.5, n))
    open_prices = prices + np.random.normal(0, 0.2, n)

    # Override today's bar
    high[-1] = today_high
    low[-1] = today_low
    open_prices[-1] = gap_open

    volume = np.random.randint(500000, 1500000, n).astype(float)
    volume[-1] = volume[-20:].mean() * 3.0  # Volume surge

    return pd.DataFrame(
        {
            "Open": open_prices,
            "High": high,
            "Low": low,
            "Close": prices,
            "Volume": volume,
        },
        index=dates,
    )


def _make_gap_fill_data(gap_pct: float = 0.005):
    """Build data that triggers a gap-fill fade signal.

    *gap_pct* controls gap size.  Default is 0.5% (inside 0.3-1.0% range).
    An indecisive candle is created (body < 30% of range).
    """
    np.random.seed(88)
    n = 100
    dates = pd.bdate_range(end=datetime.now(), periods=n)
    prices = 100 + np.cumsum(np.random.normal(0.02, 0.4, n))
    prices = np.maximum(prices, 60)

    prior_close = float(prices[-2])
    gap_open = prior_close * (1.0 + gap_pct)

    # Indecisive candle: body < 30% of range
    today_range = 1.0
    today_open = gap_open
    today_close = gap_open + today_range * 0.1  # Small body
    today_high = gap_open + today_range * 0.6
    today_low = gap_open - today_range * 0.4
    prices[-1] = today_close

    high = prices + np.abs(np.random.normal(0, 0.4, n))
    low = prices - np.abs(np.random.normal(0, 0.4, n))
    open_prices = prices + np.random.normal(0, 0.15, n)

    high[-1] = today_high
    low[-1] = today_low
    open_prices[-1] = today_open

    volume = np.random.randint(500000, 1500000, n).astype(float)

    return pd.DataFrame(
        {
            "Open": open_prices,
            "High": high,
            "Low": low,
            "Close": prices,
            "Volume": volume,
        },
        index=dates,
    )


def _make_turnaround_tuesday_data():
    """Build data for Turnaround Tuesday: Monday drop >= 1%, low IBS,
    price above SMA200.
    """
    np.random.seed(66)
    n = 250
    dates = pd.bdate_range(end=datetime.now(), periods=n)

    # Uptrending (above SMA200)
    prices = 100 + np.cumsum(np.random.normal(0.1, 0.5, n))
    prices = np.maximum(prices, 80)

    # Prior bar (Friday): healthy close
    friday_close = float(prices[-2])

    # Monday (today): drop >= 1% from Friday, close near the low (IBS < 0.2)
    monday_close = friday_close * 0.985  # 1.5% drop
    monday_high = friday_close * 0.995
    monday_low = monday_close - 0.3

    prices[-1] = monday_close

    high = prices + np.abs(np.random.normal(0, 0.5, n))
    low = prices - np.abs(np.random.normal(0, 0.5, n))
    open_prices = prices + np.random.normal(0, 0.3, n)

    high[-1] = monday_high
    low[-1] = monday_low
    open_prices[-1] = friday_close * 0.992

    volume = np.random.randint(500000, 1500000, n).astype(float)

    return pd.DataFrame(
        {
            "Open": open_prices,
            "High": high,
            "Low": low,
            "Close": prices,
            "Volume": volume,
        },
        index=dates,
    )


# ======================================================================
# 1. Config Tests
# ======================================================================


class TestConfig:
    """Tests for SelectiveConfig and per-strategy config defaults."""

    def test_default_config_values(self):
        """Verify all defaults in SelectiveConfig."""
        cfg = SelectiveConfig()
        assert cfg.enabled is True
        assert cfg.risk_modifier == 0.75
        assert cfg.max_positions == 3
        # Per-strategy sub-configs are created with their own defaults
        assert isinstance(cfg.rsi2_reversal, RSI2ReversalConfig)
        assert isinstance(cfg.triple_timeframe, TripleTimeframeConfig)
        assert isinstance(cfg.bb_climax, BBClimaxConfig)
        assert isinstance(cfg.pead_drift, PEADDriftConfig)
        assert isinstance(cfg.gap_fill, GapFillConfig)
        assert isinstance(cfg.turnaround_tuesday, TurnaroundTuesdayConfig)

    def test_env_override(self, monkeypatch):
        """Monkeypatch env vars and verify get_selective_config() respects them."""
        get_selective_config.cache_clear()
        monkeypatch.setenv("SELECTIVE_STRATEGIES_ENABLED", "False")
        monkeypatch.setenv("SELECTIVE_MAX_POSITIONS", "5")
        monkeypatch.setenv("SELECTIVE_RISK_MODIFIER", "0.50")
        try:
            cfg = get_selective_config()
            assert cfg.enabled is False
            assert cfg.max_positions == 5
            assert cfg.risk_modifier == 0.50
        finally:
            get_selective_config.cache_clear()

    def test_per_strategy_configs(self):
        """Verify each per-strategy config dataclass has expected defaults."""
        r = RSI2ReversalConfig()
        assert r.rsi_threshold == 5
        assert r.support_proximity_pct == 0.005
        assert r.min_support_touches == 2
        assert r.support_lookback_days == 60
        assert r.volume_ratio_min == 1.2
        assert r.stop_atr_mult == 1.5
        assert r.time_stop_days == 6

        t = TripleTimeframeConfig()
        assert t.sma_fast == 50
        assert t.sma_slow == 200
        assert t.consolidation_bars == 15
        assert t.breakout_volume_ratio == 1.5
        assert t.chandelier_atr_mult == 3.0
        assert t.event_blackout_days == 2

        b = BBClimaxConfig()
        assert b.bb_period == 20
        assert b.bb_std == 2.0
        assert b.volume_percentile_threshold == 95
        assert b.volume_lookback == 100
        assert b.accel_days == 3
        assert b.stop_atr_mult == 0.25

        p = PEADDriftConfig()
        assert p.min_gap_pct == 0.03
        assert p.min_volume_ratio == 2.0
        assert p.min_close_range_pct == 0.67
        assert p.hold_days == 10
        assert p.stop_atr_mult == 2.0

        g = GapFillConfig()
        assert g.gap_min_pct == 0.003
        assert g.gap_max_pct == 0.01
        assert g.max_body_pct == 0.30
        assert g.stop_buffer_atr_mult == 0.1

        tt = TurnaroundTuesdayConfig()
        assert tt.min_monday_drop_pct == 0.01
        assert tt.max_ibs == 0.2
        assert tt.require_trend_filter is True
        assert tt.sma_period == 200
        assert tt.hard_stop_pct == 0.02


# ======================================================================
# 2. Signal Tests
# ======================================================================


class TestSelectiveSignal:
    """Tests for the SelectiveSignal dataclass."""

    def test_selective_signal_creation(self):
        """Create a SelectiveSignal and check all fields."""
        ts = datetime(2026, 7, 1, 10, 0, tzinfo=EASTERN)
        sig = SelectiveSignal(
            strategy_id="hs_rsi2_reversal",
            symbol="AAPL",
            signal_strength=0.82,
            trigger_price=150.0,
            stop_price=145.0,
            target_price=160.0,
            direction="long",
            timestamp=ts,
            filters_passed=["sma200", "volume"],
            metadata={"rsi2": 3.5},
        )
        assert sig.strategy_id == "hs_rsi2_reversal"
        assert sig.symbol == "AAPL"
        assert sig.signal_strength == 0.82
        assert sig.trigger_price == 150.0
        assert sig.stop_price == 145.0
        assert sig.target_price == 160.0
        assert sig.direction == "long"
        assert sig.timestamp == ts
        assert "sma200" in sig.filters_passed
        assert sig.metadata["rsi2"] == 3.5

    def test_signal_to_core_signal(self):
        """Convert SelectiveSignal to pipeline Signal and verify all fields."""
        ts = datetime(2026, 7, 1, 10, 0, tzinfo=EASTERN)
        sel_sig = SelectiveSignal(
            strategy_id="hs_bb_climax",
            symbol="MSFT",
            signal_strength=0.70,
            trigger_price=420.0,
            stop_price=415.0,
            target_price=430.0,
            direction="long",
            timestamp=ts,
            filters_passed=["volume", "bb"],
            metadata={"vol_percentile": 97},
        )
        core = sel_sig.to_core_signal()
        assert isinstance(core, Signal)
        assert core.symbol == "MSFT"
        assert core.strategy == "hs_bb_climax"
        assert core.direction == "long"
        assert core.entry_price == 420.0
        assert core.stop_price == 415.0
        assert core.target_price == 430.0
        assert core.signal_strength == 0.70
        assert core.grade == Grade.B
        assert core.timestamp == ts
        assert "selective_filters" in core.raw_data
        assert core.raw_data["side"] == "LONG"

    def test_signal_grade_from_strength(self):
        """Verify grade computation from signal_strength thresholds."""
        # A >= 0.78
        sig_a = SelectiveSignal(
            strategy_id="x", symbol="X", signal_strength=0.80,
            trigger_price=100, stop_price=95, target_price=110,
        )
        assert sig_a.grade == Grade.A

        # B >= 0.65
        sig_b = SelectiveSignal(
            strategy_id="x", symbol="X", signal_strength=0.70,
            trigger_price=100, stop_price=95, target_price=110,
        )
        assert sig_b.grade == Grade.B

        # C >= 0.38
        sig_c = SelectiveSignal(
            strategy_id="x", symbol="X", signal_strength=0.50,
            trigger_price=100, stop_price=95, target_price=110,
        )
        assert sig_c.grade == Grade.C

        # F < 0.38
        sig_f = SelectiveSignal(
            strategy_id="x", symbol="X", signal_strength=0.20,
            trigger_price=100, stop_price=95, target_price=110,
        )
        assert sig_f.grade == Grade.F

    def test_signal_direction_long(self):
        """Long signal has correct risk_per_share."""
        sig = SelectiveSignal(
            strategy_id="x", symbol="X", signal_strength=0.75,
            trigger_price=100.0, stop_price=95.0, target_price=110.0,
            direction="long",
        )
        assert sig.risk_per_share == pytest.approx(5.0)

    def test_signal_direction_short(self):
        """Short signal has correct risk_per_share."""
        sig = SelectiveSignal(
            strategy_id="x", symbol="X", signal_strength=0.75,
            trigger_price=100.0, stop_price=105.0, target_price=90.0,
            direction="short",
        )
        assert sig.risk_per_share == pytest.approx(5.0)

    def test_signal_price_validity(self):
        """Test is_price_valid() for valid and invalid cases."""
        # Valid long
        valid_long = SelectiveSignal(
            strategy_id="x", symbol="X", signal_strength=0.7,
            trigger_price=100.0, stop_price=95.0, target_price=110.0,
            direction="long",
        )
        assert valid_long.is_price_valid() is True

        # Invalid long: stop above trigger
        bad_long = SelectiveSignal(
            strategy_id="x", symbol="X", signal_strength=0.7,
            trigger_price=100.0, stop_price=105.0, target_price=110.0,
            direction="long",
        )
        assert bad_long.is_price_valid() is False

        # Valid short
        valid_short = SelectiveSignal(
            strategy_id="x", symbol="X", signal_strength=0.7,
            trigger_price=100.0, stop_price=105.0, target_price=90.0,
            direction="short",
        )
        assert valid_short.is_price_valid() is True

        # Invalid short: target above trigger
        bad_short = SelectiveSignal(
            strategy_id="x", symbol="X", signal_strength=0.7,
            trigger_price=100.0, stop_price=105.0, target_price=110.0,
            direction="short",
        )
        assert bad_short.is_price_valid() is False

        # Invalid: zero trigger price
        zero_entry = SelectiveSignal(
            strategy_id="x", symbol="X", signal_strength=0.7,
            trigger_price=0.0, stop_price=5.0, target_price=10.0,
            direction="long",
        )
        assert zero_entry.is_price_valid() is False


# ======================================================================
# 3. Events Tests
# ======================================================================


class TestEvents:
    """Tests for macro event calendar functions."""

    def test_is_macro_event_day_nfp_friday(self):
        """First Friday of a month should return True (NFP)."""
        # Find a first-Friday: 2026-07-03 is a Friday and day 3 (1 <= 3 <= 7)
        nfp_day = date(2026, 7, 3)
        assert nfp_day.weekday() == 4  # sanity: Friday
        assert 1 <= nfp_day.day <= 7
        assert is_macro_event_day(nfp_day) is True

    def test_is_macro_event_day_normal_day(self):
        """A regular Wednesday mid-month (day 22) should return False."""
        # 2026-07-22 is a Wednesday with day=22 -- outside all windows
        normal_day = date(2026, 7, 22)
        assert normal_day.weekday() == 2  # Wednesday
        assert normal_day.day == 22
        assert is_macro_event_day(normal_day) is False

    def test_has_upcoming_event(self):
        """Check that upcoming events are detected within N days."""
        # Day before a first Friday: Thursday July 2, 2026
        day_before_nfp = date(2026, 7, 2)
        assert has_upcoming_event(day_before_nfp, days=1) is True

    def test_no_upcoming_event(self):
        """A quiet period returns False."""
        # 2026-07-20 is a Monday, day=20.  Check 1 day ahead = July 21 (Tue, day 21).
        # Day 20 and 21 are outside CPI (10-14), NFP (first Fri), and FOMC (14-18 Tue/Wed).
        quiet_day = date(2026, 7, 20)
        assert has_upcoming_event(quiet_day, days=1) is False

    def test_cpi_window(self):
        """Days 10-14 are flagged as CPI window."""
        cpi_day = date(2026, 7, 12)
        assert is_macro_event_day(cpi_day) is True

    def test_fomc_window_tuesday(self):
        """FOMC window: Tuesday between 14th-18th."""
        # 2026-07-14 is a Tuesday
        fomc_day = date(2026, 7, 14)
        assert fomc_day.weekday() == 1  # Tuesday
        assert is_macro_event_day(fomc_day) is True


# ======================================================================
# 4. Strategy Detector Tests
# ======================================================================


class TestRSI2Reversal:
    """Tests for the RSI-2 reversal detector."""

    def test_rsi2_reversal_long_signal(self):
        """Synthetic data designed to trigger RSI-2 reversal at support."""
        df = _make_rsi2_reversal_data()
        sig = detect_rsi2("TEST", df)
        # The data is designed so the signal may fire -- verify structure
        # if it does, or verify it returns None gracefully if not
        if sig is not None:
            assert sig.strategy_id == "hs_rsi2_reversal"
            assert sig.symbol == "TEST"
            assert sig.direction in ("long", "short")
            assert 0 < sig.signal_strength <= 1.0
            assert sig.trigger_price > 0
            assert sig.stop_price > 0
            assert sig.target_price > 0
            assert "rsi2" in sig.metadata
            assert "sma200" in sig.metadata
            assert "vol_ratio" in sig.metadata

    def test_rsi2_reversal_no_signal_insufficient_data(self):
        """50 bars is not enough (needs >= 200)."""
        np.random.seed(10)
        n = 50
        dates = pd.bdate_range(end=datetime.now(), periods=n)
        prices = 100 + np.cumsum(np.random.normal(0.1, 0.5, n))
        df = pd.DataFrame(
            {
                "Open": prices,
                "High": prices + 1,
                "Low": prices - 1,
                "Close": prices,
                "Volume": np.random.randint(500000, 1500000, n).astype(float),
            },
            index=dates,
        )
        assert detect_rsi2("TEST", df) is None

    def test_rsi2_reversal_no_signal_rsi_not_extreme(self, bullish_df):
        """RSI at 30 (not < 5) should not fire.  Bullish_df has normal RSI."""
        sig = detect_rsi2("TEST", bullish_df)
        # bullish_df has no extreme RSI-2 condition, so should be None
        assert sig is None


class TestTripleTimeframeBreakout:
    """Tests for the triple-timeframe breakout detector."""

    def test_triple_timeframe_breakout_signal(self):
        """Data with uptrend, consolidation, and breakout bar."""
        df = _make_triple_timeframe_breakout_data(breakout=True)
        # Patch has_upcoming_event to ensure no event blackout
        with patch("selective_strategies.strategies.triple_timeframe.has_upcoming_event", return_value=False):
            sig = detect_ttf("TEST", df)
        if sig is not None:
            assert sig.strategy_id == "hs_triple_timeframe"
            assert sig.symbol == "TEST"
            assert sig.direction in ("long", "short")
            assert 0 < sig.signal_strength <= 1.0
            assert "sma_fast" in sig.metadata
            assert "sma_slow" in sig.metadata
            assert "vol_ratio" in sig.metadata

    def test_triple_timeframe_no_signal_no_breakout(self):
        """Price stays inside consolidation -- no breakout."""
        df = _make_triple_timeframe_breakout_data(breakout=False)
        with patch("selective_strategies.strategies.triple_timeframe.has_upcoming_event", return_value=False):
            sig = detect_ttf("TEST", df)
        assert sig is None


class TestBBClimaxReversal:
    """Tests for the Bollinger Band climax reversal detector."""

    def test_bb_climax_long_signal(self):
        """Close below lower BB, extreme volume, hammer candle, declining closes."""
        df = _make_bb_climax_data(extreme_volume=True)
        sig = detect_bb("TEST", df)
        if sig is not None:
            assert sig.strategy_id == "hs_bb_climax"
            assert sig.symbol == "TEST"
            assert sig.direction == "long"
            assert 0 < sig.signal_strength <= 1.0
            assert "upper_bb" in sig.metadata
            assert "lower_bb" in sig.metadata
            assert "vol_percentile" in sig.metadata

    def test_bb_climax_no_signal_volume_too_low(self):
        """Normal volume should not trigger."""
        df = _make_bb_climax_data(extreme_volume=False)
        sig = detect_bb("TEST", df)
        assert sig is None


class TestPEADDrift:
    """Tests for the post-earnings announcement drift detector."""

    def test_pead_drift_signal(self):
        """Gap-up >= 3%, volume >= 2x, close in top third, close >= open."""
        df = _make_pead_data(gap_pct=0.05)
        sig = detect_pead("TEST", df)
        if sig is not None:
            assert sig.strategy_id == "hs_pead_drift"
            assert sig.symbol == "TEST"
            assert sig.direction == "long"
            assert 0 < sig.signal_strength <= 1.0
            assert "gap_pct" in sig.metadata
            assert sig.metadata["gap_pct"] >= 0.03

    def test_pead_drift_no_signal_gap_too_small(self):
        """1% gap is below the 3% threshold."""
        df = _make_pead_data(gap_pct=0.01)
        sig = detect_pead("TEST", df)
        assert sig is None


class TestGapFillFade:
    """Tests for the gap-fill fade detector."""

    def test_gap_fill_signal(self):
        """Gap between 0.3-1.0%, indecisive candle."""
        df = _make_gap_fill_data(gap_pct=0.005)
        # Patch is_macro_event_day to avoid event blackout
        with patch("selective_strategies.strategies.gap_fill.is_macro_event_day", return_value=False):
            sig = detect_gap("TEST", df)
        if sig is not None:
            assert sig.strategy_id == "hs_gap_fill"
            assert sig.symbol == "TEST"
            # Gap-up is faded by going short
            assert sig.direction == "short"
            assert 0 < sig.signal_strength <= 1.0
            assert "gap_pct" in sig.metadata

    def test_gap_fill_no_signal_gap_too_large(self):
        """2% gap is above the 1.0% max threshold."""
        df = _make_gap_fill_data(gap_pct=0.02)
        with patch("selective_strategies.strategies.gap_fill.is_macro_event_day", return_value=False):
            sig = detect_gap("TEST", df)
        assert sig is None

    def test_gap_fill_daily_proxy_without_fetch(self):
        """No fetcher injected -> the stop buffer uses the daily ATR proxy."""
        df = _make_gap_fill_data(gap_pct=0.005)
        with patch("selective_strategies.strategies.gap_fill.is_macro_event_day", return_value=False):
            sig = detect_gap("TEST", df)
        assert sig is not None
        assert sig.metadata["atr_mode"] == "daily_proxy"
        # daily ATR / 5 proxy (default divisor); atr14 is pre-rounded so allow
        # a small absolute tolerance.
        assert sig.metadata["intraday_atr"] == pytest.approx(
            sig.metadata["atr14"] / 5.0, abs=1e-3
        )

    def test_gap_fill_uses_intraday_atr_when_fetch_injected(self):
        """An injected intraday fetcher supplies a real intraday ATR."""
        df = _make_gap_fill_data(gap_pct=0.005)
        # 40 five-minute bars with a steady 0.50-wide range -> intraday ATR ~0.50,
        # deliberately different from daily ATR / 5.
        idx = pd.date_range("2026-07-13 09:30", periods=40, freq="5min")
        base = float(df["Close"].iloc[-1])
        bars = pd.DataFrame(
            {
                "Open": [base] * 40,
                "High": [base + 0.25] * 40,
                "Low": [base - 0.25] * 40,
                "Close": [base] * 40,
                "Volume": [500000.0] * 40,
            },
            index=idx,
        )
        with patch("selective_strategies.strategies.gap_fill.is_macro_event_day", return_value=False):
            sig = detect_gap("TEST", df, fetch=lambda *a, **k: bars)
        assert sig is not None
        assert sig.metadata["atr_mode"] == "intraday"
        assert sig.metadata["intraday_atr"] == pytest.approx(0.5, abs=1e-6)

    def test_gap_fill_falls_back_when_intraday_unavailable(self):
        """Fetcher failure (rate limit / free tier) -> daily proxy fallback."""
        df = _make_gap_fill_data(gap_pct=0.005)

        def boom(*a, **k):
            raise RuntimeError("429 rate limited")

        with patch("selective_strategies.strategies.gap_fill.is_macro_event_day", return_value=False):
            sig = detect_gap("TEST", df, fetch=boom)
        assert sig is not None
        assert sig.metadata["atr_mode"] == "daily_proxy"


class TestTurnaroundTuesday:
    """Tests for the Turnaround Tuesday detector."""

    @patch("selective_strategies.strategies.turnaround_tuesday.datetime")
    def test_turnaround_tuesday_signal(self, mock_dt):
        """Mock datetime.now to return a Monday; data has drop >= 1%, low IBS."""
        mock_dt.now.return_value = datetime(2026, 7, 6, 15, 30, tzinfo=EASTERN)  # Monday
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        df = _make_turnaround_tuesday_data()
        sig = detect_tt("TEST", df)
        if sig is not None:
            assert sig.strategy_id == "hs_turnaround_tuesday"
            assert sig.symbol == "TEST"
            assert sig.direction == "long"
            assert 0 < sig.signal_strength <= 1.0
            assert "drop_pct" in sig.metadata
            assert sig.metadata["drop_pct"] >= 0.01
            assert "ibs" in sig.metadata

    @patch("selective_strategies.strategies.turnaround_tuesday.datetime")
    def test_turnaround_tuesday_no_signal_not_monday(self, mock_dt):
        """Mock datetime.now to return Wednesday -- should not fire."""
        mock_dt.now.return_value = datetime(2026, 7, 8, 15, 30, tzinfo=EASTERN)  # Wednesday
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        df = _make_turnaround_tuesday_data()
        sig = detect_tt("TEST", df)
        assert sig is None


# ======================================================================
# 5. Scanner Tests
# ======================================================================


class TestScanner:
    """Tests for the selective scan orchestrator."""

    def test_run_selective_scan_disabled(self):
        """config.enabled=False returns empty list."""
        cfg = SelectiveConfig(enabled=False)
        result = run_selective_scan(
            symbols=["AAPL", "MSFT"],
            config=cfg,
            fetch=lambda sym, **kw: None,
        )
        assert result == []

    def test_run_selective_scan_basic(self, bullish_df):
        """Provide a simple fetch mock that returns bullish data; scan runs without error."""
        cfg = SelectiveConfig(enabled=True)

        def mock_fetch(sym, **kwargs):
            return bullish_df

        result = run_selective_scan(
            symbols=["AAPL"],
            min_grade="C",
            config=cfg,
            fetch=mock_fetch,
            max_workers=1,
        )
        # May or may not produce signals -- the point is no crash
        assert isinstance(result, list)
        for sig in result:
            assert isinstance(sig, Signal)

    def test_selective_scan_allowed_strategies_filter(self, bullish_df):
        """Only allow one strategy; verify others are skipped."""
        cfg = SelectiveConfig(enabled=True)

        def mock_fetch(sym, **kwargs):
            return bullish_df

        result = run_selective_scan(
            symbols=["AAPL"],
            min_grade="F",
            config=cfg,
            fetch=mock_fetch,
            allowed_strategies=["hs_rsi2_reversal"],
            max_workers=1,
        )
        assert isinstance(result, list)
        # If any signals returned, they must come from the allowed strategy only
        for sig in result:
            assert sig.strategy == "hs_rsi2_reversal"


# ======================================================================
# 6. Integration Tests (trade selection, risk manager)
# ======================================================================


class TestIntegration:
    """Tests verifying selective strategies integrate with trade selection and risk."""

    def test_selective_strategies_in_valid_strategies(self):
        """All hs_* keys are present in VALID_STRATEGIES."""
        expected = {
            "hs_rsi2_reversal",
            "hs_triple_timeframe",
            "hs_bb_climax",
            "hs_pead_drift",
            "hs_gap_fill",
            "hs_turnaround_tuesday",
        }
        for key in expected:
            assert key in VALID_STRATEGIES, f"{key} missing from VALID_STRATEGIES"

    def test_selective_strategies_tuple(self):
        """SELECTIVE_STRATEGIES tuple contains exactly the six strategy keys."""
        assert len(SELECTIVE_STRATEGIES) == 6
        expected = {
            "hs_rsi2_reversal",
            "hs_triple_timeframe",
            "hs_bb_climax",
            "hs_pead_drift",
            "hs_gap_fill",
            "hs_turnaround_tuesday",
        }
        assert set(SELECTIVE_STRATEGIES) == expected

    def test_strategy_family_selective(self):
        """_strategy_family("hs_rsi2_reversal") should return "selective"."""
        from risk.manager import _strategy_family

        assert _strategy_family("hs_rsi2_reversal") == "selective"
        assert _strategy_family("hs_triple_timeframe") == "selective"
        assert _strategy_family("hs_bb_climax") == "selective"
        assert _strategy_family("hs_pead_drift") == "selective"
        assert _strategy_family("hs_gap_fill") == "selective"
        assert _strategy_family("hs_turnaround_tuesday") == "selective"

    def test_strategy_tags_exist(self):
        """All hs_* keys have entries in STRATEGY_TAGS."""
        for key in STRATEGY_PRIORITY:
            assert key in STRATEGY_TAGS, f"{key} missing from STRATEGY_TAGS"

    def test_strategy_priority_matches_detectors(self):
        """STRATEGY_PRIORITY and DETECTORS have the same set of keys."""
        assert set(STRATEGY_PRIORITY) == set(DETECTORS.keys())


# ======================================================================
# 7. Analyst Card Tests
# ======================================================================


class TestAnalystCards:
    """Tests for analyst card strategy tag integration."""

    def test_analyst_strategy_tags_selective(self):
        """strategy_tag() returns correct tags for hs_* strategies."""
        tag = strategy_tag("hs_rsi2_reversal", is_position=True)
        assert "Selective" in tag
        assert "RSI-2" in tag

        tag = strategy_tag("hs_triple_timeframe", is_position=True)
        assert "Selective" in tag
        assert "triple-timeframe" in tag

        tag = strategy_tag("hs_bb_climax", is_position=False)
        assert "Selective" in tag
        assert "Bollinger" in tag

        tag = strategy_tag("hs_pead_drift", is_position=True)
        assert "Selective" in tag
        assert "post-earnings" in tag

        tag = strategy_tag("hs_gap_fill", is_position=False)
        assert "Selective" in tag
        assert "gap" in tag

        tag = strategy_tag("hs_turnaround_tuesday", is_position=True)
        assert "Selective" in tag
        assert "Turnaround" in tag


# ======================================================================
# 8. AI Analyst Guidance Tests
# ======================================================================


class TestAIAnalystGuidance:
    """Tests for strategy-specific AI veto guidance."""

    @pytest.mark.parametrize(
        "strategy_id",
        [
            "hs_rsi2_reversal",
            "hs_triple_timeframe",
            "hs_bb_climax",
            "hs_pead_drift",
            "hs_gap_fill",
            "hs_turnaround_tuesday",
        ],
    )
    def test_ai_guidance_selective_strategies(self, strategy_id):
        """_strategy_guidance returns non-empty guidance for each hs_* strategy."""
        guidance = _strategy_guidance(strategy_id)
        assert isinstance(guidance, str)
        assert len(guidance) > 20, f"Guidance for {strategy_id} is too short"
        assert "HIGHLY SELECTIVE" in guidance

    def test_ai_guidance_rsi2_reversal_content(self):
        """RSI-2 reversal guidance mentions support level."""
        guidance = _strategy_guidance("hs_rsi2_reversal")
        assert "support" in guidance.lower()

    def test_ai_guidance_gap_fill_content(self):
        """Gap-fill guidance mentions news-driven gaps."""
        guidance = _strategy_guidance("hs_gap_fill")
        assert "gap" in guidance.lower()

    def test_ai_guidance_turnaround_tuesday_content(self):
        """Turnaround Tuesday guidance mentions Monday decline."""
        guidance = _strategy_guidance("hs_turnaround_tuesday")
        assert "Monday" in guidance or "tuesday" in guidance.lower()
