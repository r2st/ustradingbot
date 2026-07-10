"""Tests for F2 — the Similar-Setup Guard (analytics.setup_similarity)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from analytics.setup_similarity import (
    BLOCK,
    DEMOTE,
    PROCEED,
    evaluate,
    find_similar_setups,
    match_similar_trades,
)
from config.settings import EASTERN, Settings
from signals.signal_types import Grade, Signal


def _sig(
    strategy: str = "momentum",
    grade: Grade = Grade.B,
    rsi: float = 68.0,
    vol: float = 1.8,
    direction: str = "long",
) -> Signal:
    return Signal(
        symbol="TSLA", strategy=strategy, direction=direction,
        entry_price=100.0, stop_price=95.0, target_price=115.0,
        signal_strength=0.70, grade=grade, rsi_value=rsi, volume_ratio=vol,
    )


def _trade(
    *, strategy="momentum", grade="B", rsi=68.0, vol=1.8, pnl=-100.0,
    r=-1.0, direction="long", days_ago=5, hold_hours=48.0,
) -> dict:
    exit_time = (datetime.now(tz=EASTERN) - timedelta(days=days_ago)).isoformat()
    return {
        "symbol": "TSLA", "strategy": strategy, "direction": direction,
        "grade": grade, "rsi_value": rsi, "volume_ratio": vol,
        "pnl_net": pnl, "r_multiple": r, "exit_time": exit_time,
        "hold_duration_hours": hold_hours,
    }


def _frame(trades) -> pd.DataFrame:
    return pd.DataFrame(trades)


# ---------------------------------------------------------------- matching

def test_matches_same_setup(settings: Settings) -> None:
    df = _frame([_trade(), _trade(rsi=70.0), _trade(vol=2.0)])
    m = match_similar_trades(
        df, strategy="momentum", direction="long", grade="B",
        rsi_value=68.0, volume_ratio=1.8, settings=settings,
    )
    assert len(m) == 3


def test_excludes_different_strategy_grade_direction(settings: Settings) -> None:
    df = _frame([
        _trade(strategy="swing"),
        _trade(grade="A"),
        _trade(direction="short"),
        _trade(),  # only this one matches a long momentum B
    ])
    m = match_similar_trades(
        df, strategy="momentum", direction="long", grade="B",
        rsi_value=68.0, volume_ratio=1.8, settings=settings,
    )
    assert len(m) == 1


def test_rsi_and_volume_tolerance(settings: Settings) -> None:
    # RSI tol is 10, vol tol is 0.5 by default.
    df = _frame([
        _trade(rsi=78.0),   # +10 → within
        _trade(rsi=79.0),   # +11 → outside
        _trade(vol=2.3),    # +0.5 → within
        _trade(vol=2.4),    # +0.6 → outside
    ])
    m = match_similar_trades(
        df, strategy="momentum", direction="long", grade="B",
        rsi_value=68.0, volume_ratio=1.8, settings=settings,
    )
    assert len(m) == 2


def test_lookback_window_excludes_old(settings: Settings) -> None:
    df = _frame([_trade(days_ago=5), _trade(days_ago=200)])
    m = match_similar_trades(
        df, strategy="momentum", direction="long", grade="B",
        rsi_value=68.0, volume_ratio=1.8, settings=settings,
    )
    assert len(m) == 1


# ---------------------------------------------------------------- evaluate

def test_insufficient_matches_proceeds(settings: Settings) -> None:
    # 3 losers < SIMILAR_SETUP_MIN_MATCHES (5) → proceed despite 0% win rate.
    df = _frame([_trade(pnl=-100.0) for _ in range(3)])
    res = evaluate(df, settings)
    assert res.action == PROCEED
    assert res.match_count == 3


def test_low_win_rate_demotes(settings: Settings) -> None:
    # 6 trades, 2 wins → 33% < 35% min but >= 15% block → demote.
    trades = [_trade(pnl=100.0, r=1.0) for _ in range(2)]
    trades += [_trade(pnl=-100.0, r=-1.0) for _ in range(4)]
    res = evaluate(_frame(trades), settings)
    assert res.action == DEMOTE
    assert 0.30 <= res.win_rate <= 0.34


def test_very_low_win_rate_blocks(settings: Settings) -> None:
    # 8 trades, 1 win → 12.5% < 15% block threshold → block.
    trades = [_trade(pnl=100.0, r=1.0)]
    trades += [_trade(pnl=-100.0, r=-1.0) for _ in range(7)]
    res = evaluate(_frame(trades), settings)
    assert res.action == BLOCK
    assert res.blocks


def test_healthy_win_rate_proceeds(settings: Settings) -> None:
    trades = [_trade(pnl=100.0, r=1.5) for _ in range(4)]
    trades += [_trade(pnl=-100.0, r=-1.0) for _ in range(2)]
    res = evaluate(_frame(trades), settings)
    assert res.action == PROCEED
    assert res.win_rate > 0.35
    assert res.avg_realized_r != 0.0


# ---------------------------------------------------------------- integration

def test_find_similar_setups_reads_csv(settings: Settings, tmp_path) -> None:
    csv = tmp_path / "trades.csv"
    trades = [_trade(pnl=100.0)]
    trades += [_trade(pnl=-100.0) for _ in range(7)]
    _frame(trades).to_csv(csv, index=False)
    res = find_similar_setups(_sig(), csv, settings)
    assert res.action == BLOCK


def test_find_similar_setups_missing_file_is_fail_open(settings: Settings, tmp_path) -> None:
    res = find_similar_setups(_sig(), tmp_path / "nope.csv", settings)
    assert res.action == PROCEED


def test_disabled_via_missing_data_proceeds(settings: Settings) -> None:
    res = evaluate(pd.DataFrame(), settings)
    assert res.action == PROCEED
    assert res.match_count == 0
