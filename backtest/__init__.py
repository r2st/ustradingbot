"""
Backtesting package for the US Trading Bot.

Replays historical daily bars through the live strategy detectors and the
paper broker's fill maths to produce an equity curve and per-trade results.

Run from the command line::

    python -m backtest --symbols AAPL,MSFT,NVDA --start 2023-01-01 --end 2024-01-01

Or programmatically::

    from backtest import BacktestConfig, run_backtest
    result = run_backtest(BacktestConfig(symbols=["AAPL"], start="2023-01-01",
                                         end="2024-01-01"))
    print(result.summary)
"""

from __future__ import annotations

from backtest.engine import (
    Backtester,
    BacktestConfig,
    BacktestResult,
    BacktestTrade,
    DEFAULT_STRATEGIES,
    run_backtest,
)
from backtest.walk_forward import (
    WalkForwardConfig,
    WalkForwardResult,
    WalkForwardWindow,
    parameter_sweep,
    run_walk_forward,
)

__all__ = [
    "Backtester",
    "BacktestConfig",
    "BacktestResult",
    "BacktestTrade",
    "DEFAULT_STRATEGIES",
    "run_backtest",
    "WalkForwardConfig",
    "WalkForwardResult",
    "WalkForwardWindow",
    "parameter_sweep",
    "run_walk_forward",
]
