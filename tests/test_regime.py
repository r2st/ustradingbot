"""Tests for market regime detection (feature 13)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.regime import (
    RegimeResult,
    current_regime,
    detect_regime,
    multiplier_for_strategy,
)
from config.settings import Settings


def _frame(prices, volume=1_000_000):
    n = len(prices)
    idx = pd.bdate_range(end="2026-06-01", periods=n)
    return pd.DataFrame(
        {"Open": prices, "High": prices, "Low": prices, "Close": prices,
         "Volume": [volume] * n},
        index=idx,
    )


def _settings(**kw):
    base = dict(REGIME_FAST_MA=50, REGIME_SLOW_MA=200, REGIME_VOL_WINDOW=20,
                REGIME_HIGH_VOL_PCT=0.018)
    base.update(kw)
    return Settings(**base)


def test_bull_regime(bullish_df):
    r = detect_regime(bullish_df, _settings())
    assert r.regime == "bull"
    assert r.weight_multipliers["momentum"] > r.weight_multipliers["swing"]


def test_bear_regime(bearish_df):
    r = detect_regime(bearish_df, _settings())
    assert r.regime == "bear"
    assert r.weight_multipliers["swing"] >= r.weight_multipliers["momentum"]


def test_sideways_regime():
    # A long decline with a recent shallow bounce: the last price pokes above
    # the slow MA while the fast MA is still below it -> mixed -> sideways.
    prices = list(np.linspace(120, 95, 245)) + list(np.linspace(95, 106, 15))
    r = detect_regime(_frame(prices), _settings(REGIME_HIGH_VOL_PCT=1.0))
    assert r.regime == "sideways"


def test_insufficient_history():
    r = detect_regime(_frame([100] * 50), _settings())
    assert r.reason == "insufficient history"
    assert r.weight_multipliers == {"momentum": 1.0, "swing": 1.0}


def test_high_volatility_lowers_multipliers():
    prices = list(np.linspace(100, 200, 260))  # strong uptrend -> bull
    calm = detect_regime(_frame(prices), _settings(REGIME_HIGH_VOL_PCT=1.0))
    volatile = detect_regime(_frame(prices), _settings(REGIME_HIGH_VOL_PCT=0.0))
    assert volatile.volatility == "high"
    assert volatile.weight_multipliers["momentum"] < calm.weight_multipliers["momentum"]


def test_multipliers_clamped():
    prices = list(np.linspace(100, 400, 260))
    r = detect_regime(_frame(prices), _settings())
    for v in r.weight_multipliers.values():
        assert 0.3 <= v <= 1.5


def test_multiplier_for_strategy():
    r = RegimeResult(weight_multipliers={"momentum": 1.2, "swing": 0.8})
    assert multiplier_for_strategy("vcp_breakout", r) == 1.2
    assert multiplier_for_strategy("mean_reversion", r) == 0.8
    assert multiplier_for_strategy("unknown", r) == 1.0


def test_current_regime_disabled():
    r = current_regime(Settings(REGIME_DETECTION_ENABLED=False), fetcher=lambda s: None)
    assert r.weight_multipliers == {"momentum": 1.0, "swing": 1.0}


def test_current_regime_fetch_error_neutral():
    def boom(_s):
        raise RuntimeError("no data")

    r = current_regime(_settings(REGIME_DETECTION_ENABLED=True), fetcher=boom)
    assert r.weight_multipliers == {"momentum": 1.0, "swing": 1.0}
