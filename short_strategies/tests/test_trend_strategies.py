"""Unit tests for the trend-following short strategies (1-4)."""

from __future__ import annotations

import pytest

from short_strategies.common.config import (
    AdxFilterConfig,
    BearFlagConfig,
    MaCrossunderConfig,
    SharedFilterConfig,
    SupportBreakdownConfig,
)
from short_strategies.strategies import (
    adx_filter,
    bear_flag,
    ma_crossunder,
    support_breakdown,
)
from short_strategies.tests.conftest import (
    downtrend_closes,
    make_df,
    uptrend_closes,
)


# ---------------------------------------------------------------------------
# 1. Support breakdown
# ---------------------------------------------------------------------------


def _support_breakdown_df(break_today: bool = True, volume_spike: bool = True):
    """Price bounces off ~95 support three times, then breaks below it."""
    closes, lows = [], []
    base = [100, 98, 96, 95.2, 97, 99, 98, 96, 95.1, 97, 99, 100, 98, 96,
            95.3, 97, 99]
    pattern = base * 4  # ~68 bars of range-bound trade over support at ~95
    for c in pattern:
        closes.append(float(c))
        lows.append(float(c) - 0.8)
    if break_today:
        closes.append(93.5)  # decisive close below the 95 support zone
        lows.append(93.0)
    else:
        closes.append(96.0)
        lows.append(95.4)
    n = len(closes)
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) + 0.8 for o, c in zip(opens, closes)]
    vols = [1_000_000.0] * n
    if volume_spike:
        vols[-1] = 2_500_000.0
    return make_df(closes, open_=opens, high=highs, low=lows, volume=vols)


class TestSupportBreakdown:
    CFG = SupportBreakdownConfig()

    def test_detects_break_with_volume(self, loose_filters):
        df = _support_breakdown_df()
        sig = support_breakdown.detect("TEST", df, self.CFG, loose_filters)
        assert sig is not None
        assert sig.strategy_id == "short_support_breakdown"
        assert sig.stop_price > sig.trigger_price > sig.target_price
        assert sig.metadata["volume_ratio"] >= 1.5

    def test_no_signal_without_volume(self, loose_filters):
        df = _support_breakdown_df(volume_spike=False)
        assert support_breakdown.detect("TEST", df, self.CFG, loose_filters) is None

    def test_no_signal_without_break(self, loose_filters):
        df = _support_breakdown_df(break_today=False, volume_spike=True)
        assert support_breakdown.detect("TEST", df, self.CFG, loose_filters) is None

    def test_atr_floor_rejects_quiet_names(self):
        df = _support_breakdown_df()
        strict = SharedFilterConfig(min_atr_pct=0.5)  # absurd floor
        assert support_breakdown.detect("TEST", df, self.CFG, strict) is None


# ---------------------------------------------------------------------------
# 2. MA crossunder
# ---------------------------------------------------------------------------


def _crossunder_df():
    """Uptrend rolling over: fresh EMA20-under-SMA50 cross near the end."""
    closes = uptrend_closes(120, drift=0.004, seed=5)
    # Sharp rollover produces the crossunder within the last bars.
    last = closes[-1]
    closes += [last * (1 - 0.02 * i) for i in range(1, 18)]
    return make_df(closes)


class TestMaCrossunder:
    CFG = MaCrossunderConfig()

    def test_detects_fresh_crossunder(self, loose_filters):
        # Scan the rollover window for the bar where the cross is fresh —
        # the detector must fire on exactly the bars where the crossunder
        # happened within cross_within_bars.
        df = _crossunder_df()
        hits = []
        for i in range(60, len(df)):
            sig = ma_crossunder.detect("TEST", df.iloc[:i], self.CFG, loose_filters)
            if sig is not None:
                hits.append((i, sig))
        assert hits, "no crossunder detected anywhere in the rollover"
        _, sig = hits[0]
        assert sig.strategy_id == "short_ma_crossunder"
        assert sig.stop_price > sig.trigger_price

    def test_stale_cross_not_detected(self, loose_filters):
        # Deep in a long-established downtrend the cross is ancient history.
        df = make_df(downtrend_closes(250))
        cfg = MaCrossunderConfig(cross_within_bars=3)
        assert ma_crossunder.detect("TEST", df, cfg, loose_filters) is None

    def test_uptrend_not_detected(self, uptrend_df, loose_filters):
        assert ma_crossunder.detect("TEST", uptrend_df, self.CFG, loose_filters) is None


# ---------------------------------------------------------------------------
# 3. Bear flag
# ---------------------------------------------------------------------------


def _bear_flag_df(breakdown: bool = True):
    """Stable base, then a sharp 12% pole, a 5-bar drifting-up flag, and a
    breakdown bar under the flag low."""
    closes = [100.0 + 0.05 * i for i in range(60)]  # quiet base near 103
    top = closes[-1]
    # Pole: five hard down days (~12%).
    pole = [top * (1 - 0.025 * i) for i in range(1, 6)]
    closes += pole
    flag_base = pole[-1]
    # Flag: five bars drifting slightly up.
    flag = [flag_base * (1 + 0.004 * i) for i in range(1, 6)]
    closes += flag
    if breakdown:
        closes.append(min(flag) * 0.985)
    else:
        closes.append(flag[-1] * 1.001)
    return make_df(closes)


class TestBearFlag:
    CFG = BearFlagConfig()

    def test_detects_flag_breakdown(self, loose_filters):
        sig = bear_flag.detect("TEST", _bear_flag_df(), self.CFG, loose_filters)
        assert sig is not None
        assert sig.strategy_id == "short_bear_flag"
        assert sig.metadata["pole_drop_pct"] >= self.CFG.pole_min_drop_pct
        assert sig.stop_price > sig.trigger_price

    def test_no_signal_while_flag_holds(self, loose_filters):
        sig = bear_flag.detect(
            "TEST", _bear_flag_df(breakdown=False), self.CFG, loose_filters
        )
        assert sig is None

    def test_no_signal_in_plain_uptrend(self, uptrend_df, loose_filters):
        assert bear_flag.detect("TEST", uptrend_df, self.CFG, loose_filters) is None


# ---------------------------------------------------------------------------
# 4. ADX confirmation filter
# ---------------------------------------------------------------------------


class TestAdxFilter:
    def test_confirms_strong_downtrend(self):
        df = make_df(downtrend_closes(120, drift=-0.008, seed=3))
        assert adx_filter.confirms_downtrend(df) is True

    def test_rejects_uptrend(self):
        df = make_df(uptrend_closes(120, drift=0.008, seed=3))
        assert adx_filter.confirms_downtrend(df) is False

    def test_fails_closed_on_insufficient_data(self):
        assert adx_filter.confirms_downtrend(make_df([100.0] * 10)) is False

    def test_standalone_disabled_by_default(self, loose_filters):
        df = make_df(downtrend_closes(120, drift=-0.008, seed=3))
        assert adx_filter.detect("TEST", df, None, loose_filters) is None

    def test_standalone_fires_when_enabled(self, loose_filters):
        df = make_df(downtrend_closes(120, drift=-0.008, seed=3))
        cfg = AdxFilterConfig(standalone_enabled=True)
        sig = adx_filter.detect("TEST", df, cfg, loose_filters)
        assert sig is not None
        assert sig.metadata["minus_di"] > sig.metadata["plus_di"]
