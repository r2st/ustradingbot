"""Tests for strategy auto-tuning (feature 12)."""

from __future__ import annotations

import pandas as pd

from automation.autotune import compute_adjustment, tune
from config.settings import Settings


def _settings(**kw):
    base = dict(AUTOTUNE_ENABLED=True, AUTOTUNE_MIN_TRADES=10,
                AUTOTUNE_LOOKBACK_TRADES=30, AUTOTUNE_MAX_GRADE_ADJUST=0.08)
    base.update(kw)
    return Settings(**base)


def _trades(pnls):
    return pd.DataFrame({
        "pnl_net": [str(p) for p in pnls],
        "exit_time": [f"2026-06-{i % 28 + 1:02d}T15:00:00" for i in range(len(pnls))],
    })


def test_disabled():
    r = tune(_trades([1] * 20), Settings(AUTOTUNE_ENABLED=False))
    assert not r.applied and r.reason == "disabled"


def test_insufficient_trades():
    r = tune(_trades([1, -1, 1]), _settings())
    assert not r.applied and "insufficient" in r.reason


def test_cold_streak_raises():
    # 2 winners / 18 losers -> win rate 0.1 (cold)
    r = tune(_trades([100, 100] + [-50] * 18), _settings())
    assert r.applied and r.delta > 0
    assert r.thresholds["A"] > 0.78


def test_hot_streak_relaxes():
    r = tune(_trades([100] * 18 + [-50, -50]), _settings())  # win rate 0.9
    assert r.applied and r.delta < 0
    assert r.thresholds["A"] < 0.78


def test_neutral_no_change():
    r = tune(_trades([100, -50] * 10), _settings())  # win rate 0.5
    assert not r.applied and r.delta == 0.0


def test_delta_clamped():
    r = tune(_trades([-50] * 20), _settings(AUTOTUNE_MAX_GRADE_ADJUST=0.05))  # win rate 0
    assert abs(r.delta) <= 0.05


def test_thresholds_ordered_and_bounded():
    r = tune(_trades([-50] * 30), _settings(AUTOTUNE_MAX_GRADE_ADJUST=0.5))
    t = r.thresholds
    assert t["A"] > t["B"] > t["C"]
    assert all(0.05 <= v <= 0.95 for v in t.values())


def test_compute_adjustment_midpoint_zero():
    assert compute_adjustment(0.5, _settings()) == 0.0
    assert compute_adjustment(0.0, _settings()) > 0
    assert compute_adjustment(1.0, _settings()) < 0
