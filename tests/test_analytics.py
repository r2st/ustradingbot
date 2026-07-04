"""Tests for the performance analytics module."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from analytics.performance import (
    analyze_journal,
    breakdown_by,
    build_equity_curve,
    build_report,
    compute_metrics,
    expectancy,
    load_completed_trades,
    max_drawdown,
    profit_factor,
    sharpe_ratio,
    sortino_ratio,
    win_rate,
)


def _trades() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"symbol": "AAPL", "strategy": "momentum", "pnl_net": 100.0,
             "r_multiple": 1.5, "exit_time": "2024-01-02T16:00:00"},
            {"symbol": "MSFT", "strategy": "swing", "pnl_net": -40.0,
             "r_multiple": -1.0, "exit_time": "2024-01-03T16:00:00"},
            {"symbol": "AAPL", "strategy": "momentum", "pnl_net": 60.0,
             "r_multiple": 0.9, "exit_time": "2024-01-04T16:00:00"},
            {"symbol": "NVDA", "strategy": "momentum", "pnl_net": -20.0,
             "r_multiple": -0.5, "exit_time": "2024-01-05T16:00:00"},
        ]
    )


# ------------------------------------------------------------ scalar metrics


def test_win_rate() -> None:
    assert win_rate([1, -1, 2, -3]) == 0.5
    assert win_rate([]) == 0.0


def test_profit_factor() -> None:
    # wins 3, losses 1 -> 3.0
    assert profit_factor([2, 1, -1]) == pytest.approx(3.0)


def test_profit_factor_no_losses_is_inf() -> None:
    assert math.isinf(profit_factor([1, 2, 3]))
    assert profit_factor([-1, -2]) == 0.0


def test_expectancy() -> None:
    assert expectancy([10, -5, 10, -5]) == pytest.approx(2.5)


def test_max_drawdown() -> None:
    # Peak 120, trough 90 -> abs 30, pct 25%.
    dd = max_drawdown([100, 120, 90, 110])
    assert dd["abs"] == pytest.approx(30.0)
    assert dd["pct"] == pytest.approx(0.25)


def test_max_drawdown_monotonic_is_zero() -> None:
    dd = max_drawdown([100, 110, 120, 130])
    assert dd["abs"] == 0.0
    assert dd["pct"] == 0.0


def test_sharpe_and_sortino_positive_for_uptrend() -> None:
    returns = [0.01, 0.02, -0.005, 0.015, 0.008]
    assert sharpe_ratio(returns) > 0
    assert sortino_ratio(returns) > 0


def test_sharpe_zero_for_too_few_points() -> None:
    assert sharpe_ratio([0.01]) == 0.0


def test_sortino_inf_when_no_downside() -> None:
    assert math.isinf(sortino_ratio([0.01, 0.02, 0.03]))


# ------------------------------------------------------------ aggregation


def test_compute_metrics_totals() -> None:
    m = compute_metrics(_trades(), starting_capital=10_000.0)
    assert m["total_trades"] == 4
    assert m["wins"] == 2 and m["losses"] == 2
    assert m["win_rate"] == 0.5
    assert m["total_pnl"] == pytest.approx(100.0)
    assert m["ending_equity"] == pytest.approx(10_100.0)
    assert m["expectancy"] == pytest.approx(25.0)
    # profit factor = 160 / 60
    assert m["profit_factor"] == pytest.approx(160.0 / 60.0, rel=1e-3)


def test_compute_metrics_empty() -> None:
    m = compute_metrics(pd.DataFrame(), starting_capital=5_000.0)
    assert m["total_trades"] == 0
    assert m["profit_factor"] is None
    assert m["ending_equity"] == 5_000.0


def test_equity_curve_cumulates() -> None:
    curve = build_equity_curve(_trades(), starting_capital=10_000.0)
    assert len(curve) == 4
    assert curve[0]["equity"] == pytest.approx(10_100.0)
    assert curve[-1]["equity"] == pytest.approx(10_100.0)  # net +100 overall


def test_breakdown_by_strategy() -> None:
    rows = breakdown_by(_trades(), "strategy", 10_000.0)
    by_name = {r["strategy"]: r for r in rows}
    assert by_name["momentum"]["trades"] == 3
    assert by_name["momentum"]["total_pnl"] == pytest.approx(140.0)
    assert by_name["swing"]["trades"] == 1


def test_breakdown_by_symbol() -> None:
    rows = breakdown_by(_trades(), "symbol", 10_000.0)
    by_name = {r["symbol"]: r for r in rows}
    assert by_name["AAPL"]["trades"] == 2
    assert by_name["AAPL"]["total_pnl"] == pytest.approx(160.0)


def test_build_report_shape() -> None:
    report = build_report(_trades(), 10_000.0)
    d = report.to_dict()
    assert set(d) == {"summary", "by_strategy", "by_symbol", "equity_curve", "recent_trades"}
    assert len(d["recent_trades"]) == 4


# ------------------------------------------------------------ journal integration


def test_load_and_analyze_real_journal(settings, sample_trade_order, sample_exit_event) -> None:
    """End-to-end: write real journal rows, then analyse them."""
    from journal.trade_logger import TradeLogger

    journal = TradeLogger(str(settings.DATA_DIR))
    journal.log_entry(sample_trade_order, fill_price=195.50, commission=0.05)
    journal.log_exit("AAPL", sample_exit_event, exit_commission=0.05)

    completed = load_completed_trades(journal.csv_path)
    assert len(completed) == 1
    assert completed.iloc[0]["symbol"] == "AAPL"

    report = analyze_journal(journal.csv_path, starting_capital=12_000.0)
    assert report.summary["total_trades"] == 1
    # sample exit is a stop loss -> a losing trade.
    assert report.summary["losses"] == 1


def test_analyze_missing_journal_is_empty(tmp_path) -> None:
    report = analyze_journal(tmp_path / "nope.csv", starting_capital=1_000.0)
    assert report.summary["total_trades"] == 0
    assert report.equity_curve == []
