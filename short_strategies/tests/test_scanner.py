"""Tests for the short-scan orchestrator."""

from __future__ import annotations

from typing import Optional

import pandas as pd

from short_strategies.common.config import ShortModuleConfig, SharedFilterConfig
from short_strategies.risk.filters import ShortFilterChain
from short_strategies.scanner import run_short_scan
from short_strategies.tests.conftest import make_df
from short_strategies.tests.test_reversal_strategies import _gap_fail_df


def _loose_module_config(**filter_overrides) -> ShortModuleConfig:
    cfg = ShortModuleConfig()
    cfg.filters = SharedFilterConfig(min_atr_pct=0.0, **filter_overrides)
    return cfg


def _offline_chain(cfg: ShortModuleConfig) -> ShortFilterChain:
    return ShortFilterChain(
        cfg.filters,
        total_capital=12_000.0,
        short_pct_float_fn=lambda s: 0.05,
        next_earnings_fn=lambda s: None,
        regime_ok_fn=lambda: (True, "bear"),
    )


def _fetch_for(frames: dict):
    """A fetch stub: 260 flat bars pad symbols so MIN_OHLCV_ROWS passes."""

    def fetch(symbol: str, period: str = "2y") -> Optional[pd.DataFrame]:
        return frames.get(symbol)

    return fetch


def _pad(df: pd.DataFrame, rows: int = 260) -> pd.DataFrame:
    """Left-pad *df* with flat bars so it clears MIN_OHLCV_ROWS (200)."""
    if len(df) >= rows:
        return df
    from datetime import datetime

    first_close = float(df["Close"].iloc[0])
    pad_n = rows - len(df)
    pad = make_df([first_close] * pad_n)
    out = pd.concat([pad, df]).reset_index(drop=True)
    out.index = pd.bdate_range(end=datetime.now(), periods=len(out))
    return out


class TestRunShortScan:
    def test_disabled_module_returns_empty(self):
        cfg = _loose_module_config()
        cfg.enabled = False
        assert run_short_scan(["AAA"], config=cfg,
                              fetch=_fetch_for({})) == []

    def test_gap_fail_signal_flows_to_core_signal(self):
        cfg = _loose_module_config()
        cfg.adx_confirm_enabled = True  # gap_fail is not trend-following
        frames = {"GAP": _pad(_gap_fail_df())}
        signals = run_short_scan(
            ["GAP"], min_grade="C", config=cfg,
            fetch=_fetch_for(frames), filter_chain=_offline_chain(cfg),
            open_positions={},
        )
        assert len(signals) == 1
        core = signals[0]
        assert core.direction == "short"
        assert core.strategy == "short_gap_fail"
        assert core.stop_price > core.entry_price > core.target_price
        assert "borrow_locate" in core.raw_data["filters_passed"]

    def test_adx_vetoes_trend_following_strategy(self):
        # A symbol whose ONLY setup is trend-following (ma_crossunder) in a
        # sideways chop: ADX confirmation off -> maybe fires; on -> vetoed.
        cfg = _loose_module_config()
        cfg.adx_confirm_enabled = True
        # Flat/choppy series never produces ADX-confirmed downtrend, and the
        # detector needs a fresh crossunder; use a rollover that is too mild
        # for ADX but crosses the MAs.
        closes = [100.0 + (0.3 if i % 2 else -0.3) for i in range(240)]
        frames = {"CHOP": _pad(make_df(closes))}
        signals = run_short_scan(
            ["CHOP"], min_grade="C", config=cfg,
            fetch=_fetch_for(frames), filter_chain=_offline_chain(cfg),
            open_positions={},
        )
        assert all(s.strategy not in (
            "short_ma_crossunder", "short_support_breakdown",
        ) for s in signals)

    def test_min_grade_respected(self):
        cfg = _loose_module_config()
        frames = {"GAP": _pad(_gap_fail_df())}
        signals = run_short_scan(
            ["GAP"], min_grade="A", config=cfg,
            fetch=_fetch_for(frames), filter_chain=_offline_chain(cfg),
            open_positions={},
        )
        for s in signals:
            assert s.grade.value == "A"

    def test_exposure_cap_admits_strongest_first(self):
        cfg = _loose_module_config(max_short_exposure_pct=0.12)
        frames = {
            "GAP1": _pad(_gap_fail_df()),
            "GAP2": _pad(_gap_fail_df()),
            "GAP3": _pad(_gap_fail_df()),
        }
        signals = run_short_scan(
            list(frames), min_grade="C", config=cfg,
            fetch=_fetch_for(frames), filter_chain=_offline_chain(cfg),
            open_positions={},
        )
        # The cap (0.12 x 12k = $1,440) admits exactly one ~$1.2k position.
        assert len(signals) == 1

    def test_allowed_strategies_whitelist(self):
        cfg = _loose_module_config()
        frames = {"GAP": _pad(_gap_fail_df())}
        signals = run_short_scan(
            ["GAP"], min_grade="C", config=cfg,
            allowed_strategies=["short_bear_flag"],
            fetch=_fetch_for(frames), filter_chain=_offline_chain(cfg),
            open_positions={},
        )
        assert signals == []

    def test_fetch_failure_is_contained(self):
        cfg = _loose_module_config()

        def broken_fetch(symbol, period="2y"):
            raise RuntimeError("provider down")

        assert run_short_scan(
            ["AAA"], config=cfg, fetch=broken_fetch,
            filter_chain=_offline_chain(cfg),
        ) == []
