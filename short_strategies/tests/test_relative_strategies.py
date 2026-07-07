"""Unit tests for the relative-weakness strategies (5-6)."""

from __future__ import annotations

from short_strategies.common.config import (
    LaggardFadeConfig,
    RelativeWeaknessConfig,
)
from short_strategies.common.context import MarketContext, build_market_context
from short_strategies.strategies import laggard_fade, relative_weakness
from short_strategies.tests.conftest import downtrend_closes, make_df


def _ctx_with_returns(returns: dict, sectors: dict | None = None,
                      benchmark_df=None) -> MarketContext:
    ctx = MarketContext(benchmark_df=benchmark_df)
    ctx.returns = dict(returns)
    ctx.sector_by_symbol = sectors or {s: "Tech" for s in returns}
    return ctx


class TestRelativeWeakness:
    CFG = RelativeWeaknessConfig(min_peers=5, bottom_decile=0.10,
                                 require_below_ma=True)

    def _returns(self, laggard_ret: float = -0.30):
        # 20 peers: 19 spread between -2% and +8%, plus one deep laggard.
        rets = {f"P{i}": -0.02 + 0.005 * i for i in range(19)}
        rets["LAG"] = laggard_ret
        return rets

    def test_bottom_decile_laggard_detected(self, loose_filters):
        df = make_df(downtrend_closes(120))  # below its 50-day MA
        ctx = _ctx_with_returns(self._returns())
        sig = relative_weakness.detect("LAG", df, self.CFG, loose_filters, ctx)
        assert sig is not None
        assert sig.strategy_id == "short_relative_weakness"
        assert sig.metadata["rank"] == 1

    def test_median_peer_not_detected(self, loose_filters):
        df = make_df(downtrend_closes(120))
        ctx = _ctx_with_returns(self._returns())
        assert relative_weakness.detect("P9", df, self.CFG, loose_filters, ctx) is None

    def test_requires_context(self, loose_filters):
        df = make_df(downtrend_closes(120))
        assert relative_weakness.detect("LAG", df, self.CFG, loose_filters, None) is None

    def test_above_ma_blocks(self, loose_filters):
        # A laggard by trailing return that now trades above its 50-day MA
        # (sharp V recovery) is not shorted.
        closes = downtrend_closes(100)
        recovery = [closes[-1] * (1 + 0.03 * i) for i in range(1, 31)]
        df = make_df(closes + recovery)
        ctx = _ctx_with_returns(self._returns())
        assert relative_weakness.detect("LAG", df, self.CFG, loose_filters, ctx) is None

    def test_sector_fallback_to_universe(self, loose_filters):
        # LAG is alone in its sector -> ranking falls back to all symbols.
        df = make_df(downtrend_closes(120))
        sectors = {f"P{i}": "Tech" for i in range(19)}
        sectors["LAG"] = "Energy"
        ctx = _ctx_with_returns(self._returns(), sectors)
        sig = relative_weakness.detect("LAG", df, self.CFG, loose_filters, ctx)
        assert sig is not None
        assert sig.metadata["peers"] == 20


class TestLaggardFade:
    CFG = LaggardFadeConfig()

    def _market_down_ctx(self, bench_drop=-0.02, my_day_ret=-0.045):
        bench = make_df([500.0] * 30 + [500.0 * (1 + bench_drop)])
        ctx = MarketContext(benchmark_df=bench)
        ctx.day_returns = {"WEAK": my_day_ret}
        ctx.returns = {"WEAK": -0.1}
        return ctx

    def _weak_day_df(self, day_ret=-0.045, close_low=True):
        closes = [100.0] * 40
        last = closes[-1] * (1 + day_ret)
        closes.append(last)
        opens = [closes[0]] + closes[:-1]
        highs = [max(o, c) * 1.012 for o, c in zip(opens, closes)]
        lows = [min(o, c) * 0.988 for o, c in zip(opens, closes)]
        if not close_low:
            # Close at the top of a wide range instead.
            lows[-1] = last * 0.90
        return make_df(closes, open_=opens, high=highs, low=lows)

    def test_laggard_on_down_day_detected(self, loose_filters):
        ctx = self._market_down_ctx()
        sig = laggard_fade.detect(
            "WEAK", self._weak_day_df(), self.CFG, loose_filters, ctx
        )
        assert sig is not None
        assert sig.strategy_id == "short_laggard_fade"
        assert sig.metadata["underperformance"] >= self.CFG.underperform_pct

    def test_no_signal_when_market_flat(self, loose_filters):
        ctx = self._market_down_ctx(bench_drop=0.0)
        assert laggard_fade.detect(
            "WEAK", self._weak_day_df(), self.CFG, loose_filters, ctx
        ) is None

    def test_no_signal_when_not_underperforming(self, loose_filters):
        ctx = self._market_down_ctx(bench_drop=-0.02, my_day_ret=-0.022)
        assert laggard_fade.detect(
            "WEAK", self._weak_day_df(day_ret=-0.022), self.CFG, loose_filters, ctx
        ) is None

    def test_no_signal_when_close_off_lows(self, loose_filters):
        ctx = self._market_down_ctx()
        df = self._weak_day_df(close_low=False)
        assert laggard_fade.detect("WEAK", df, self.CFG, loose_filters, ctx) is None


class TestBuildMarketContext:
    def test_returns_and_day_returns_computed(self):
        frames = {
            "A": make_df([100.0] * 30 + [90.0]),
            "B": make_df([100.0] * 30 + [110.0]),
        }
        ctx = build_market_context(
            ["A", "B"], frames, benchmark_df=make_df([500.0] * 31),
            rank_lookback_days=20, sector_lookup=lambda s: "Tech",
        )
        assert ctx.day_returns["A"] < 0 < ctx.day_returns["B"]
        assert ctx.returns["A"] < ctx.returns["B"]
        assert ctx.benchmark_day_return == 0.0
