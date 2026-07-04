"""
Monte Carlo projection of future account equity (feature 8).

Bootstraps the *empirical* distribution of historical per-trade net P&L: for
each simulated run it draws ``horizon`` trades with replacement from the
realised ``pnl_net`` series, compounds them onto the starting capital, and
collects the ending equity and the run's worst drawdown.  Aggregating thousands
of runs yields confidence intervals (percentiles) for where the account could
be after the next ``horizon`` trades.

The core :func:`simulate` is a pure function (numpy only) so it unit-tests
without any I/O; :func:`run_from_journal` wires it to the live trade journal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_PCT_KEYS = ["p5", "p10", "p25", "p50", "p75", "p90", "p95"]
_PCT_Q = [5, 10, 25, 50, 75, 90, 95]


@dataclass
class MonteCarloResult:
    """Aggregated outcome of a Monte Carlo equity projection."""

    runs: int
    horizon: int
    trades_sampled: int
    starting_capital: float
    mean_ending: float = 0.0
    median_ending: float = 0.0
    std_ending: float = 0.0
    percentiles: Dict[str, float] = field(default_factory=dict)
    prob_profit: float = 0.0
    prob_loss_10pct: float = 0.0
    max_drawdown_p50: float = 0.0
    sample_paths: List[List[float]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "runs": self.runs,
            "horizon": self.horizon,
            "trades_sampled": self.trades_sampled,
            "starting_capital": round(self.starting_capital, 2),
            "mean_ending": round(self.mean_ending, 2),
            "median_ending": round(self.median_ending, 2),
            "std_ending": round(self.std_ending, 2),
            "percentiles": {k: round(v, 2) for k, v in self.percentiles.items()},
            "prob_profit": round(self.prob_profit, 4),
            "prob_loss_10pct": round(self.prob_loss_10pct, 4),
            "max_drawdown_p50": round(self.max_drawdown_p50, 4),
            "sample_paths": [[round(v, 2) for v in path] for path in self.sample_paths],
        }


def _empty(starting_capital: float, runs: int, horizon: int) -> MonteCarloResult:
    return MonteCarloResult(
        runs=runs,
        horizon=horizon,
        trades_sampled=0,
        starting_capital=float(starting_capital),
        mean_ending=float(starting_capital),
        median_ending=float(starting_capital),
        percentiles={k: float(starting_capital) for k in _PCT_KEYS},
    )


def simulate(
    pnl_samples: Sequence[float],
    starting_capital: float,
    runs: int = 1000,
    horizon: int = 50,
    seed: Optional[int] = 42,
    n_sample_paths: int = 20,
) -> MonteCarloResult:
    """Bootstrap *runs* equity paths of *horizon* trades from *pnl_samples*.

    Args:
        pnl_samples: Historical per-trade net P&L (dollars).
        starting_capital: Equity at the start of every simulated path.
        runs: Number of Monte Carlo paths.
        horizon: Trades per path.
        seed: RNG seed for reproducibility (``None`` for nondeterministic).
        n_sample_paths: How many full equity paths to retain for charting.

    Returns:
        A :class:`MonteCarloResult`.  When *pnl_samples* is empty the result is
        well-formed but zeroed (``trades_sampled == 0``).
    """
    samples = np.asarray([float(x) for x in pnl_samples], dtype=float)
    samples = samples[np.isfinite(samples)]
    runs = max(1, int(runs))
    horizon = max(1, int(horizon))
    if samples.size == 0:
        return _empty(starting_capital, runs, horizon)

    rng = np.random.default_rng(seed)
    # draws[run, step] — a full runs x horizon matrix of bootstrapped P&L.
    draws = rng.choice(samples, size=(runs, horizon), replace=True)
    equity = float(starting_capital) + np.cumsum(draws, axis=1)
    equity = np.hstack([np.full((runs, 1), float(starting_capital)), equity])

    ending = equity[:, -1]
    running_peak = np.maximum.accumulate(equity, axis=1)
    drawdowns = np.where(running_peak > 0, (running_peak - equity) / running_peak, 0.0)
    max_dd_per_run = drawdowns.max(axis=1)

    percentiles = {
        key: float(np.percentile(ending, q)) for key, q in zip(_PCT_KEYS, _PCT_Q)
    }
    return MonteCarloResult(
        runs=runs,
        horizon=horizon,
        trades_sampled=int(samples.size),
        starting_capital=float(starting_capital),
        mean_ending=float(ending.mean()),
        median_ending=float(np.median(ending)),
        std_ending=float(ending.std()),
        percentiles=percentiles,
        prob_profit=float((ending > starting_capital).mean()),
        prob_loss_10pct=float((ending < 0.9 * starting_capital).mean()),
        max_drawdown_p50=float(np.percentile(max_dd_per_run, 50)),
        sample_paths=[equity[i].tolist() for i in range(min(n_sample_paths, runs))],
    )


def run_from_journal(
    settings,
    runs: Optional[int] = None,
    horizon: Optional[int] = None,
) -> MonteCarloResult:
    """Run a Monte Carlo projection from the live trade journal.  Never raises."""
    from pathlib import Path

    runs = int(runs if runs is not None else getattr(settings, "MONTE_CARLO_RUNS", 1000))
    horizon = int(
        horizon if horizon is not None else getattr(settings, "MONTE_CARLO_HORIZON", 50)
    )
    starting_capital = float(getattr(settings, "TOTAL_CAPITAL", 0.0))
    try:
        import pandas as pd

        from analytics.performance import load_completed_trades

        df = load_completed_trades(Path(settings.DATA_DIR) / "trades.csv")
        if df.empty or "pnl_net" not in df.columns:
            return _empty(starting_capital, runs, horizon)
        pnl = pd.to_numeric(df["pnl_net"], errors="coerce").dropna().tolist()
    except Exception as exc:  # noqa: BLE001
        log.warning("montecarlo.journal_load_failed", error=str(exc))
        return _empty(starting_capital, runs, horizon)
    return simulate(pnl, starting_capital, runs=runs, horizon=horizon)
