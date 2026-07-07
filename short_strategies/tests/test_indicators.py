"""Unit tests for the shared indicator library."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from short_strategies.common.indicators import (
    adx,
    atr,
    pct_return,
    rolling_vwap,
    rsi,
    trend_state,
    volume_ratio,
)
from short_strategies.tests.conftest import (
    downtrend_closes,
    make_df,
    uptrend_closes,
)


class TestAtr:
    def test_positive_on_normal_data(self):
        df = make_df(downtrend_closes(60))
        value = atr(df)
        assert value is not None and value > 0

    def test_none_on_insufficient_data(self):
        df = make_df([100.0] * 5)
        assert atr(df) is None

    def test_matches_hand_computed_constant_range(self):
        # Constant 2-point daily range and flat closes -> ATR == 2.
        n = 30
        close = [100.0] * n
        df = make_df(close, open_=[100.0] * n, high=[101.0] * n, low=[99.0] * n)
        assert atr(df) == pytest.approx(2.0, abs=1e-9)


class TestRsi:
    def test_extreme_on_straight_advance(self):
        close = pd.Series(np.linspace(100, 150, 40))
        assert rsi(close) > 90

    def test_extreme_low_on_straight_decline(self):
        close = pd.Series(np.linspace(150, 100, 40))
        assert rsi(close) < 10

    def test_neutral_default_on_short_series(self):
        assert rsi(pd.Series([100.0, 101.0])) == 50.0


class TestAdx:
    def test_downtrend_has_minus_di_dominant(self):
        df = make_df(downtrend_closes(120, drift=-0.008, seed=3))
        values = adx(df)
        assert values is not None
        assert values["minus_di"] > values["plus_di"]
        assert values["adx"] > 15

    def test_uptrend_has_plus_di_dominant(self):
        df = make_df(uptrend_closes(120, drift=0.008, seed=3))
        values = adx(df)
        assert values is not None
        assert values["plus_di"] > values["minus_di"]

    def test_none_on_insufficient_data(self):
        assert adx(make_df([100.0] * 10)) is None


class TestRollingVwap:
    def test_weighted_by_volume(self):
        # Two-price series with all the volume on the 90.0 bars pulls the
        # VWAP well below the arithmetic mean of typical prices.
        close = [110.0] * 10 + [90.0] * 10
        vol = [1.0] * 10 + [1_000_000.0] * 10
        df = make_df(close, volume=vol)
        vwap = rolling_vwap(df, window=20)
        assert vwap is not None
        assert vwap < 92.0

    def test_none_on_zero_volume(self):
        df = make_df([100.0] * 25, volume=[0.0] * 25)
        assert rolling_vwap(df, window=20) is None


class TestVolumeRatio:
    def test_spike_detected(self):
        vol = [1_000_000.0] * 59 + [3_000_000.0]
        df = make_df([100.0] * 60, volume=vol)
        assert volume_ratio(df) == pytest.approx(3.0, rel=1e-6)


class TestTrendState:
    def test_downtrend(self, downtrend_df):
        assert trend_state(downtrend_df) == "down"

    def test_uptrend(self, uptrend_df):
        assert trend_state(uptrend_df) == "up"

    def test_flat_on_insufficient_data(self):
        assert trend_state(make_df([100.0] * 10)) == "flat"


class TestPctReturn:
    def test_simple_return(self):
        close = pd.Series([100.0, 100.0, 110.0])
        assert pct_return(close, 1) == pytest.approx(0.10)

    def test_none_when_short(self):
        assert pct_return(pd.Series([100.0]), 5) is None
