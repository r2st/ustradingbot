"""Tests for the short-strategy backtest harness."""

from __future__ import annotations

from short_strategies.common.config import (
    SharedFilterConfig,
    ShortModuleConfig,
)
from short_strategies.backtests.runner import run_short_backtest
from short_strategies.common.signal import ShortSignal
from short_strategies.tests.conftest import make_df


def _cfg() -> ShortModuleConfig:
    cfg = ShortModuleConfig()
    cfg.filters = SharedFilterConfig(min_atr_pct=0.0)
    return cfg


def _detector_fire_once(fired: dict):
    """A stub detector that signals exactly once, then stays quiet."""

    def detect(symbol, df, config=None, filters=None, ctx=None):
        if fired.get("done"):
            return None
        fired["done"] = True
        price = float(df["Close"].iloc[-1])
        return ShortSignal(
            strategy_id="short_gap_fail",
            symbol=symbol,
            signal_strength=0.8,
            trigger_price=price,
            stop_price=price * 1.05,
            target_price=price * 0.90,
        )

    return detect


class TestRunShortBacktest:
    def test_target_hit_produces_winning_trade(self):
        # Price collapses after the signal -> the cover target is hit.
        closes = [100.0] * 130 + [100, 97, 94, 91, 88, 85]
        df = make_df(closes)
        result = run_short_backtest(
            "TEST", df, "short_gap_fail", config=_cfg(),
            detector=_detector_fire_once({}), warmup_bars=128,
        )
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.exit_reason == "TARGET_HIT"
        assert trade.pnl > 0
        assert result.win_rate == 1.0

    def test_stop_hit_produces_losing_trade(self):
        # Price rips higher after the signal -> the buy-stop is hit.
        closes = [100.0] * 130 + [100, 103, 107, 111]
        df = make_df(closes)
        result = run_short_backtest(
            "TEST", df, "short_gap_fail", config=_cfg(),
            detector=_detector_fire_once({}), warmup_bars=128,
        )
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.exit_reason == "STOP_HIT"
        assert trade.pnl < 0
        assert trade.r_multiple < 0

    def test_time_exit_after_max_hold(self):
        # Price goes nowhere -> the time exit closes the trade.
        closes = [100.0] * 160
        df = make_df(closes)
        result = run_short_backtest(
            "TEST", df, "short_gap_fail", config=_cfg(),
            detector=_detector_fire_once({}), warmup_bars=128,
            max_hold_bars=5,
        )
        assert len(result.trades) == 1
        assert result.trades[0].exit_reason == "TIME_EXIT"

    def test_empty_result_on_short_history(self):
        result = run_short_backtest(
            "TEST", make_df([100.0] * 50), "short_gap_fail", config=_cfg(),
        )
        assert result.trades == []

    def test_real_detector_runs_end_to_end(self):
        # Smoke: the registry detector over a synthetic frame must not raise.
        closes = [100.0] * 200
        result = run_short_backtest(
            "TEST", make_df(closes), "short_gap_fail", config=_cfg(),
        )
        assert result.summary()["strategy"] == "short_gap_fail"
