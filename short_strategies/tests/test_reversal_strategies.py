"""Unit tests for the reversal/exhaustion strategies (7-11)."""

from __future__ import annotations

import pandas as pd
import pytest

from short_strategies.common.config import (
    BuyingClimaxConfig,
    EarningsPopFadeConfig,
    GapFailConfig,
    OverboughtFadeConfig,
    VwapRejectionConfig,
)
from short_strategies.strategies import (
    buying_climax,
    earnings_pop_fade,
    gap_fail,
    overbought_fade,
    vwap_rejection,
)
from short_strategies.tests.conftest import (
    downtrend_closes,
    make_df,
    uptrend_closes,
)


# ---------------------------------------------------------------------------
# 7. Overbought fade
# ---------------------------------------------------------------------------


def _overbought_rejection_df(rejection: bool = True):
    """Straight advance into a prior resistance zone, then a rejection candle.

    Highs derive from the close only (not ``max(open, close)``) so the base's
    swing highs are strict fractal pivots — an open equal to the prior peak
    close would otherwise tie the neighbouring bar's high and erase the pivot.
    """
    # Base with pivots near 130 (resistance).
    base = []
    for _ in range(4):
        base += [120, 124, 128, 129.8, 128, 124, 120, 116, 118, 122]
    # Pullback, then a gains-only advance back into the zone (pins RSI high).
    pullback = [118.0, 114.0, 110.0]
    advance = [110 + 0.8 * i for i in range(1, 24)]  # ends ~128.4
    closes = [float(c) for c in base] + pullback + advance
    opens = [closes[0]] + closes[:-1]
    highs = [c * 1.005 for c in closes]
    lows = [min(o, c) * 0.995 for o, c in zip(opens, closes)]
    if rejection:
        # Rejection bar: spikes to 131 (tags resistance), closes near the low.
        opens.append(128.4)
        highs.append(131.0)
        lows.append(127.0)
        closes.append(127.8)
    else:
        # Strong close at the high.
        opens.append(128.4)
        highs.append(131.0)
        lows.append(128.2)
        closes.append(130.9)
    return make_df(closes, open_=opens, high=highs, low=lows)


class TestOverboughtFade:
    # rsi_min lowered for the synthetic frame: Wilder's EWM remembers the
    # base's losses, capping RSI near 69 here.  The threshold itself is
    # config; the negative case asserts normal-RSI frames stay quiet.
    CFG = OverboughtFadeConfig(rsi_min=65.0)

    def test_detects_rejection_at_resistance(self, loose_filters):
        sig = overbought_fade.detect(
            "TEST", _overbought_rejection_df(), self.CFG, loose_filters
        )
        assert sig is not None
        assert sig.strategy_id == "short_overbought_fade"
        assert sig.metadata["rsi"] >= self.CFG.rsi_min
        assert sig.stop_price > sig.trigger_price

    def test_no_signal_on_strong_close(self, loose_filters):
        assert overbought_fade.detect(
            "TEST", _overbought_rejection_df(rejection=False),
            self.CFG, loose_filters,
        ) is None

    def test_no_signal_when_rsi_normal(self, downtrend_df, loose_filters):
        assert overbought_fade.detect(
            "TEST", downtrend_df, self.CFG, loose_filters
        ) is None


# ---------------------------------------------------------------------------
# 8. Gap fail
# ---------------------------------------------------------------------------


def _gap_fail_df(gap=0.05, close_below_open=True):
    closes = [100.0] * 40
    opens = [closes[0]] + closes[:-1]
    highs = [c * 1.012 for c in closes]
    lows = [c * 0.988 for c in closes]
    gap_open = 100.0 * (1 + gap)
    opens.append(gap_open)
    highs.append(gap_open * 1.01)
    if close_below_open:
        closes.append(gap_open * 0.97)  # gave the gap back
        lows.append(gap_open * 0.965)
    else:
        closes.append(gap_open * 1.005)  # held the gap
        lows.append(gap_open * 0.998)
    return make_df(closes, open_=opens, high=highs, low=lows)


class TestGapFail:
    CFG = GapFailConfig(gap_min_pct=0.03)

    def test_detects_failed_gap(self, loose_filters):
        sig = gap_fail.detect("TEST", _gap_fail_df(), self.CFG, loose_filters)
        assert sig is not None
        assert sig.strategy_id == "short_gap_fail"
        assert sig.metadata["gap_pct"] >= 0.03
        assert sig.stop_price > sig.trigger_price > sig.target_price

    def test_no_signal_when_gap_holds(self, loose_filters):
        assert gap_fail.detect(
            "TEST", _gap_fail_df(close_below_open=False), self.CFG, loose_filters
        ) is None

    def test_no_signal_on_small_gap(self, loose_filters):
        assert gap_fail.detect(
            "TEST", _gap_fail_df(gap=0.01), self.CFG, loose_filters
        ) is None

    def test_below_prior_close_requirement(self, loose_filters):
        cfg = GapFailConfig(gap_min_pct=0.03, require_below_prior_close=True)
        # Fails the stricter check: close (101.85) is above prior close (100).
        df = _gap_fail_df(gap=0.05)
        assert df["Close"].iloc[-1] > 100.0
        assert gap_fail.detect("TEST", df, cfg, loose_filters) is None


# ---------------------------------------------------------------------------
# 9. Earnings pop fade
# ---------------------------------------------------------------------------


def _pop_fade_df(giveback: float = 0.7):
    closes = [100.0] * 40
    opens = [closes[0]] + closes[:-1]
    # Earnings pop day: gaps to 108, tags 109.
    pop_open = 108.0
    opens.append(pop_open)
    closes.append(pop_open - giveback * (pop_open - 100.0))
    highs = [max(o, c) * 1.01 for o, c in zip(opens, closes)]
    lows = [min(o, c) * 0.99 for o, c in zip(opens, closes)]
    highs[-1] = 109.0
    return make_df(closes, open_=opens, high=highs, low=lows)


class TestEarningsPopFade:
    CFG = EarningsPopFadeConfig(days_after_earnings=3, gap_min_pct=0.05,
                                fade_min_pct=0.5)

    def test_detects_faded_pop(self, loose_filters):
        sig = earnings_pop_fade.detect(
            "TEST", _pop_fade_df(), self.CFG, loose_filters,
            days_since_earnings_fn=lambda s: 0,
        )
        assert sig is not None
        assert sig.strategy_id == "short_earnings_pop_fade"
        assert sig.metadata["giveback"] >= 0.5

    def test_no_signal_when_pop_holds(self, loose_filters):
        assert earnings_pop_fade.detect(
            "TEST", _pop_fade_df(giveback=0.2), self.CFG, loose_filters,
            days_since_earnings_fn=lambda s: 0,
        ) is None

    def test_no_signal_outside_earnings_window(self, loose_filters):
        assert earnings_pop_fade.detect(
            "TEST", _pop_fade_df(), self.CFG, loose_filters,
            days_since_earnings_fn=lambda s: 30,
        ) is None

    def test_fail_closed_without_earnings_data(self, loose_filters):
        assert earnings_pop_fade.detect(
            "TEST", _pop_fade_df(), self.CFG, loose_filters,
            days_since_earnings_fn=lambda s: None,
        ) is None


# ---------------------------------------------------------------------------
# 10. VWAP rejection
# ---------------------------------------------------------------------------


def _vwap_rejection_df():
    """Downtrend, then a bounce whose last bar tags the rolling VWAP and
    closes back below it."""
    closes = downtrend_closes(200, drift=-0.005, seed=11)
    from short_strategies.common.indicators import rolling_vwap

    # Bounce toward the VWAP over a few bars.
    df = make_df(closes)
    vwap = rolling_vwap(df, 20)
    assert vwap is not None
    last = closes[-1]
    bounce = [last * (1 + 0.01 * i) for i in range(1, 4)]
    closes = closes + bounce
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) * 1.005 for o, c in zip(opens, closes)]
    lows = [min(o, c) * 0.995 for o, c in zip(opens, closes)]
    # Rejection bar: high pokes above the rolling VWAP, close well below it.
    df2 = make_df(closes, open_=opens, high=highs, low=lows)
    vwap2 = rolling_vwap(df2, 20)
    opens.append(closes[-1])
    highs.append(vwap2 * 1.01)
    closes.append(vwap2 * 0.97)
    lows.append(vwap2 * 0.965)
    return make_df(closes, open_=opens, high=highs, low=lows)


class TestVwapRejection:
    CFG = VwapRejectionConfig()

    def test_detects_rejection_in_downtrend(self, loose_filters):
        sig = vwap_rejection.detect(
            "TEST", _vwap_rejection_df(), self.CFG, loose_filters
        )
        assert sig is not None
        assert sig.strategy_id == "short_vwap_rejection"
        assert sig.trigger_price < sig.metadata["vwap"]

    def test_no_signal_in_uptrend(self, uptrend_df, loose_filters):
        assert vwap_rejection.detect(
            "TEST", uptrend_df, self.CFG, loose_filters
        ) is None

    def test_no_signal_when_bounce_holds_above_vwap(self, loose_filters):
        # A downtrend whose last close reclaims the VWAP is not a rejection.
        closes = downtrend_closes(200, drift=-0.005, seed=11)
        from short_strategies.common.indicators import rolling_vwap

        df = make_df(closes)
        vwap = rolling_vwap(df, 20)
        closes = closes + [vwap * 1.02]
        assert vwap_rejection.detect(
            "TEST", make_df(closes), self.CFG, loose_filters
        ) is None

    def test_defaults_to_daily_mode_without_fetch(self, loose_filters):
        # No fetcher injected -> daily-approximation VWAP, tagged in metadata.
        sig = vwap_rejection.detect(
            "TEST", _vwap_rejection_df(), self.CFG, loose_filters
        )
        assert sig is not None
        assert sig.metadata["vwap_mode"] == "daily"

    def test_uses_intraday_vwap_when_fetch_injected(self, loose_filters):
        # Inject an intraday fetcher whose VWAP sits just above the last close,
        # reproducing the rejection.  Metadata reports the intraday mode and the
        # VWAP value comes from the intraday bars, not the daily frame.
        df = _vwap_rejection_df()
        last_close = float(df["Close"].iloc[-1])
        intraday_vwap_level = last_close * 1.03

        idx = pd.date_range("2026-07-13 09:30", periods=6, freq="5min")
        bars = pd.DataFrame(
            {
                "Open": [intraday_vwap_level] * 6,
                "High": [intraday_vwap_level * 1.001] * 6,
                "Low": [intraday_vwap_level * 0.999] * 6,
                "Close": [intraday_vwap_level] * 6,
                "Volume": [1_000_000.0] * 6,
            },
            index=idx,
        )
        sig = vwap_rejection.detect(
            "TEST", df, self.CFG, loose_filters,
            fetch=lambda *a, **k: bars,
        )
        assert sig is not None
        assert sig.metadata["vwap_mode"] == "intraday"
        assert sig.metadata["vwap"] == pytest.approx(intraday_vwap_level, rel=1e-3)

    def test_falls_back_to_daily_when_intraday_unavailable(self, loose_filters):
        # Fetcher raises (rate limit / free tier) -> graceful daily fallback.
        def boom(*a, **k):
            raise RuntimeError("429 rate limited")
        sig = vwap_rejection.detect(
            "TEST", _vwap_rejection_df(), self.CFG, loose_filters, fetch=boom
        )
        assert sig is not None
        assert sig.metadata["vwap_mode"] == "daily"


# ---------------------------------------------------------------------------
# 11. Buying climax
# ---------------------------------------------------------------------------


def _climax_df(reversal: bool = True):
    """40% advance, a huge-volume new-high climax bar, then a reversal bar
    closing below the climax midpoint."""
    closes = list(uptrend_closes(80, drift=0.006, seed=21))
    top = closes[-1]
    vols = [1_000_000.0] * len(closes)
    # Climax bar: +6% on 5x volume, new high.
    climax_close = top * 1.06
    closes.append(climax_close)
    vols.append(5_000_000.0)
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) * 1.008 for o, c in zip(opens, closes)]
    lows = [min(o, c) * 0.992 for o, c in zip(opens, closes)]
    # Reversal bar.
    climax_high = highs[-1]
    climax_low = lows[-1]
    mid = (climax_high + climax_low) / 2
    opens.append(climax_close)
    if reversal:
        closes.append(mid * 0.97)
        lows.append(mid * 0.965)
    else:
        closes.append(climax_close * 1.01)
        lows.append(climax_close * 0.995)
    highs.append(climax_close * 1.012)
    vols.append(2_000_000.0)
    return make_df(closes, open_=opens, high=highs, low=lows, volume=vols)


class TestBuyingClimax:
    CFG = BuyingClimaxConfig(advance_min_pct=0.15, climax_volume_ratio=3.0)

    def test_detects_post_climax_reversal(self, loose_filters):
        sig = buying_climax.detect("TEST", _climax_df(), self.CFG, loose_filters)
        assert sig is not None
        assert sig.strategy_id == "short_buying_climax"
        assert sig.metadata["climax_volume_ratio"] >= 3.0
        assert sig.stop_price > sig.trigger_price

    def test_no_signal_without_reversal(self, loose_filters):
        assert buying_climax.detect(
            "TEST", _climax_df(reversal=False), self.CFG, loose_filters
        ) is None

    def test_no_signal_without_climactic_volume(self, loose_filters):
        df = _climax_df()
        df["Volume"] = 1_000_000.0  # flatten the volume spike
        assert buying_climax.detect("TEST", df, self.CFG, loose_filters) is None
