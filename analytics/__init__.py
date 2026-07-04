"""Performance analytics for the US Trading Bot.

Reads the trade journal (or any normalised trade set) and computes portfolio
performance statistics: win rate, profit factor, expectancy, Sharpe/Sortino
ratios, max drawdown, average R multiple, per-strategy and per-symbol
breakdowns, and an equity curve.
"""

from __future__ import annotations

from analytics.performance import (
    PerformanceReport,
    analyze_journal,
    build_equity_curve,
    compute_metrics,
    load_completed_trades,
    max_drawdown,
    metrics_from_records,
    profit_factor,
    sharpe_ratio,
    sortino_ratio,
    win_rate,
)

__all__ = [
    "PerformanceReport",
    "analyze_journal",
    "build_equity_curve",
    "compute_metrics",
    "load_completed_trades",
    "max_drawdown",
    "metrics_from_records",
    "profit_factor",
    "sharpe_ratio",
    "sortino_ratio",
    "win_rate",
]
