"""Tests for walk-forward analysis and parameter optimization (P0-2)."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from backtest import BacktestConfig, WalkForwardConfig, run_walk_forward
from backtest.walk_forward import (
    _apply_params,
    detect_overfitting,
    generate_windows,
    parameter_sweep,
)
from config.settings import Settings


def _make_df(seed: int, n: int = 900, drift: float = 0.0012) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=datetime(2024, 6, 1), periods=n)
    rets = rng.normal(drift, 0.02, n)
    price = 100 * np.exp(np.cumsum(rets))
    high = price * (1 + np.abs(rng.normal(0, 0.012, n)))
    low = price * (1 - np.abs(rng.normal(0, 0.012, n)))
    op = price * (1 + rng.normal(0, 0.005, n))
    vol = rng.integers(800_000, 3_000_000, n).astype(float)
    return pd.DataFrame(
        {"Open": op, "High": high, "Low": low, "Close": price, "Volume": vol},
        index=dates,
    )


@pytest.fixture
def data() -> dict:
    return {sym: _make_df(i) for i, sym in enumerate(["AAA", "BBB", "CCC", "DDD"])}


@pytest.fixture
def wf_config(data) -> WalkForwardConfig:
    df = data["AAA"]
    return WalkForwardConfig(
        symbols=list(data),
        start=df.index[260],
        end=df.index[-1],
        train_months=12,
        test_months=3,
        step_months=3,
        min_grade="C",
    )


# ---------------------------------------------------------------------------
# Window generation
# ---------------------------------------------------------------------------


class TestWindows:
    def test_contiguous_non_overlapping_test_windows(self) -> None:
        wins = generate_windows("2020-01-01", "2023-01-01", 12, 3, 3)
        assert len(wins) >= 4
        # Each window: train is 12 months, test follows train, is 3 months.
        for tr_s, tr_e, te_s, te_e in wins:
            assert te_s == tr_e  # test starts where train ends
            assert (te_e - te_s).days >= 88  # ~3 months
        # Consecutive test windows are contiguous (step == test length).
        assert wins[1][2] == wins[0][2] + pd.DateOffset(months=3)

    def test_no_truncated_final_window(self) -> None:
        wins = generate_windows("2020-01-01", "2021-06-15", 12, 3, 3)
        # train 12 + test 3 = 15 months; only one full window fits by mid-2021.
        assert len(wins) == 1
        assert wins[0][3] <= pd.Timestamp("2021-06-16")

    def test_invalid_months_raise(self) -> None:
        with pytest.raises(ValueError):
            generate_windows("2020-01-01", "2021-01-01", 0, 3)
        with pytest.raises(ValueError):
            generate_windows("2020-01-01", "2021-01-01", 12, -1)


# ---------------------------------------------------------------------------
# Parameter application + sweep
# ---------------------------------------------------------------------------


class TestParams:
    def test_apply_splits_config_and_settings(self) -> None:
        base_cfg = BacktestConfig(symbols=["AAA"], start="2023-01-01", end="2023-06-01")
        base_settings = Settings()
        cfg, sett = _apply_params(
            base_cfg,
            base_settings,
            {"max_positions": 5, "RISK_REWARD_MIN": 2.5},
        )
        assert cfg.max_positions == 5  # config field applied
        assert sett.RISK_REWARD_MIN == 2.5  # settings field applied
        assert base_settings.RISK_REWARD_MIN != 2.5  # original untouched

    def test_apply_ignores_unknown_settings_keys(self) -> None:
        base_cfg = BacktestConfig(symbols=["AAA"], start="2023-01-01", end="2023-06-01")
        cfg, sett = _apply_params(base_cfg, Settings(), {"NOT_A_REAL_SETTING": 1})
        assert cfg is base_cfg  # nothing changed

    def test_sweep_runs_all_combos_sorted(self, data) -> None:
        df = data["AAA"]
        cfg = BacktestConfig(
            symbols=list(data), start=df.index[500], end=df.index[-1], min_grade="C"
        )
        grid = {"min_grade": ["B", "C"], "max_positions": [5, 25]}
        rows = parameter_sweep(cfg, grid, data, Settings(), objective="total_return_pct")
        assert len(rows) == 4  # 2 x 2
        # sorted descending by objective
        objectives = [r["objective"] for r in rows]
        assert objectives == sorted(objectives, reverse=True)
        assert all("params" in r and "summary" in r for r in rows)


# ---------------------------------------------------------------------------
# Overfitting detection
# ---------------------------------------------------------------------------


class TestOverfitting:
    def test_flags_collapse(self) -> None:
        out = detect_overfitting(
            {"sharpe_ratio": 2.0, "total_return_pct": 30.0},
            {"sharpe_ratio": -0.5, "total_return_pct": -10.0},
        )
        assert out["warning"] is True
        assert out["degradation_ratio"] > 0.5
        assert out["reasons"]

    def test_no_warning_when_holds_up(self) -> None:
        out = detect_overfitting(
            {"sharpe_ratio": 1.5, "total_return_pct": 20.0},
            {"sharpe_ratio": 1.4, "total_return_pct": 18.0},
        )
        assert out["warning"] is False
        assert out["degradation_ratio"] < 0.5


# ---------------------------------------------------------------------------
# End-to-end driver
# ---------------------------------------------------------------------------


class TestDriver:
    def test_run_walk_forward_structure(self, wf_config, data) -> None:
        result = run_walk_forward(wf_config, data=data, settings=Settings())
        assert result.windows, "expected at least one walk-forward window"
        for w in result.windows:
            assert w.train_end == w.test_start
            assert "total_trades" in w.in_sample
            assert "total_trades" in w.out_of_sample
        # Aggregates + overfitting report are always present.
        assert "total_trades" in result.aggregate_oos
        assert "total_trades" in result.aggregate_is
        assert "warning" in result.overfitting
        # Serialisable for the dashboard.
        d = result.to_dict()
        assert d["windows"] and "aggregate_oos" in d

    def test_run_walk_forward_with_grid(self, wf_config, data) -> None:
        wf_config.param_grid = {"min_grade": ["B", "C"]}
        result = run_walk_forward(wf_config, data=data, settings=Settings())
        for w in result.windows:
            # Each window recorded the best grade it optimized to + all scores.
            assert w.best_params.get("min_grade") in {"B", "C"}
            assert len(w.grid_scores) == 2
