"""
Daily-bar walk-forward backtest harness for the short strategies.

A deliberately lightweight, dependency-free runner: for each bar after the
warm-up window it hands the detector the history *up to that bar*, opens a
simulated short on a signal (side-aware slippage via the shared paper-broker
fill maths), and manages it with the ATR buy-stop, cover target, and a
max-hold time exit.  Good for parameter sanity checks and relative strategy
comparison — not a tick-accurate execution simulator.

Usage::

    from short_strategies.backtests.runner import run_short_backtest
    result = run_short_backtest("AAPL", df, "short_gap_fail")
    print(result.summary())
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
import structlog

from execution.broker import (
    commission_for,
    short_entry_slippage,
    short_stop_exit_fill,
    short_target_exit_fill,
)
from short_strategies.common.config import ShortModuleConfig, get_short_config
from short_strategies.strategies import DETECTORS

log = structlog.get_logger(__name__)

#: Bars of history a detector sees before the walk-forward starts.
WARMUP_BARS = 120

#: Calendar-bar cap on how long a backtest short stays open.
MAX_HOLD_BARS = 15


@dataclass
class ShortTrade:
    """One simulated short round-trip."""

    symbol: str
    strategy_id: str
    entry_date: str
    entry_price: float
    stop_price: float
    target_price: float
    quantity: int
    exit_date: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl: float = 0.0
    r_multiple: float = 0.0


@dataclass
class ShortBacktestResult:
    """Aggregate statistics for one strategy over one symbol."""

    symbol: str
    strategy_id: str
    trades: List[ShortTrade] = field(default_factory=list)

    @property
    def total_pnl(self) -> float:
        return round(sum(t.pnl for t in self.trades), 2)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.pnl > 0)
        return round(wins / len(self.trades), 4)

    @property
    def avg_r(self) -> float:
        if not self.trades:
            return 0.0
        return round(sum(t.r_multiple for t in self.trades) / len(self.trades), 4)

    def summary(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy_id,
            "trades": len(self.trades),
            "win_rate": self.win_rate,
            "avg_r": self.avg_r,
            "total_pnl": self.total_pnl,
        }


def run_short_backtest(
    symbol: str,
    df: pd.DataFrame,
    strategy_id: str,
    config: Optional[ShortModuleConfig] = None,
    quantity: int = 100,
    slippage_bps: float = 5.0,
    commission_per_share: float = 0.005,
    warmup_bars: int = WARMUP_BARS,
    max_hold_bars: int = MAX_HOLD_BARS,
    detector: Optional[Callable] = None,
) -> ShortBacktestResult:
    """Walk *df* forward, trading *strategy_id* signals short.

    Args:
        symbol: Ticker (labelling only).
        df: Full-history daily OHLCV frame.
        strategy_id: Key into the strategy registry (e.g. ``"short_gap_fail"``).
        config: Module config override.
        quantity: Fixed share count per trade (relative comparison, so
            constant size keeps the statistics interpretable).
        slippage_bps / commission_per_share: Fill frictions (paper-broker
            defaults).
        warmup_bars: History bars before the first tradeable signal.
        max_hold_bars: Time exit after this many bars in the trade.
        detector: Detector override (defaults to the registry entry).

    Returns:
        A :class:`ShortBacktestResult` with per-trade detail.
    """
    cfg = config or get_short_config()
    fn = detector or DETECTORS[strategy_id]
    result = ShortBacktestResult(symbol=symbol, strategy_id=strategy_id)
    if df is None or len(df) <= warmup_bars + 1:
        return result

    open_trade: Optional[ShortTrade] = None
    bars_held = 0

    strategy_cfg = getattr(cfg, strategy_id.removeprefix("short_"), None)

    for i in range(warmup_bars, len(df) - 1):
        history = df.iloc[: i + 1]
        next_bar = df.iloc[i + 1]
        next_date = str(df.index[i + 1].date()) if hasattr(df.index[i + 1], "date") else str(i + 1)

        if open_trade is not None:
            bars_held += 1
            bar_open = float(next_bar["Open"])
            bar_high = float(next_bar["High"])
            bar_low = float(next_bar["Low"])

            exit_price = None
            reason = ""
            # Conservative ordering: the buy-stop fills before the target
            # when both sides are touched in one bar.
            if bar_high >= open_trade.stop_price:
                exit_price = short_stop_exit_fill(
                    open_trade.stop_price, bar_open, slippage_bps
                )
                reason = "STOP_HIT"
            elif bar_low <= open_trade.target_price:
                exit_price = short_target_exit_fill(
                    open_trade.target_price, bar_open
                )
                reason = "TARGET_HIT"
            elif bars_held >= max_hold_bars:
                exit_price = float(next_bar["Close"])
                reason = "TIME_EXIT"

            if exit_price is not None:
                open_trade.exit_date = next_date
                open_trade.exit_price = round(exit_price, 4)
                open_trade.exit_reason = reason
                gross = (open_trade.entry_price - exit_price) * open_trade.quantity
                fees = 2 * commission_for(open_trade.quantity, commission_per_share)
                open_trade.pnl = round(gross - fees, 2)
                risk = (
                    open_trade.stop_price - open_trade.entry_price
                ) * open_trade.quantity
                open_trade.r_multiple = (
                    round(open_trade.pnl / risk, 4) if risk > 0 else 0.0
                )
                result.trades.append(open_trade)
                open_trade = None
                bars_held = 0
            continue

        sig = fn(symbol, history, config=strategy_cfg, filters=cfg.filters)
        if sig is None or not sig.is_price_valid():
            continue
        fill = short_entry_slippage(float(next_bar["Open"]), slippage_bps)
        open_trade = ShortTrade(
            symbol=symbol,
            strategy_id=strategy_id,
            entry_date=next_date,
            entry_price=round(fill, 4),
            stop_price=sig.stop_price,
            target_price=sig.target_price,
            quantity=quantity,
        )
        bars_held = 0

    log.info("short_backtest.complete", **result.summary())
    return result
