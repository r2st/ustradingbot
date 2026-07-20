"""
Performance attribution (P1-10).

Decomposes realised P&L three ways:

* **Sector attribution** — how much P&L came from each sector.
* **Strategy attribution** — how much from each strategy (completes the existing
  :func:`analytics.performance.breakdown_by` with contribution shares).
* **Factor attribution** — a market-beta decomposition of the equity curve into
  a *systematic* (market-driven) and a *specific* (alpha/idiosyncratic) part.

The functions accept a trades DataFrame (and, for the factor split, aligned
return series) so they unit-test without any network access.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from analytics.performance import breakdown_by
from config.universe import get_sector


def _contribution_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Annotate per-group rows with each group's share of gross P&L magnitude."""
    gross = sum(abs(float(r.get("total_pnl", 0.0))) for r in rows) or 1.0
    total = sum(float(r.get("total_pnl", 0.0)) for r in rows)
    for r in rows:
        pnl = float(r.get("total_pnl", 0.0))
        r["contribution_pct"] = round(pnl / total * 100, 2) if total else 0.0
        r["gross_share_pct"] = round(abs(pnl) / gross * 100, 2)
    return rows


def sector_attribution(trades: pd.DataFrame) -> List[Dict[str, Any]]:
    """Return realised P&L attributed to each sector, largest contributor first."""
    if trades is None or trades.empty or "symbol" not in trades.columns:
        return []
    df = trades.copy()
    df["sector"] = df["symbol"].astype(str).map(get_sector)
    rows = breakdown_by(df, "sector", 0.0)
    return _contribution_rows(rows)


def strategy_attribution(
    trades: pd.DataFrame, starting_capital: float = 0.0
) -> List[Dict[str, Any]]:
    """Return realised P&L attributed to each strategy with contribution shares."""
    rows = breakdown_by(trades, "strategy", starting_capital)
    return _contribution_rows(rows)


def factor_attribution(
    portfolio_returns: Sequence[float] | pd.Series,
    market_returns: Sequence[float] | pd.Series,
    total_pnl: float = 0.0,
) -> Dict[str, Any]:
    """Market-beta decomposition of a return series into systematic + specific.

    Regresses the portfolio's periodic returns on the market's (SPY) via
    ``beta = cov / var`` and ``alpha = mean(port) - beta * mean(market)``.  The
    systematic share of return is ``beta * mean(market)`` and the specific share
    is ``alpha``; ``total_pnl`` is split into dollar contributions on those
    shares.  Returns zeros with ``observations == 0`` when there is too little
    aligned history.
    """
    p = pd.Series(list(portfolio_returns), dtype=float).reset_index(drop=True)
    m = pd.Series(list(market_returns), dtype=float).reset_index(drop=True)
    n = min(len(p), len(m))
    p, m = p.iloc[:n].dropna(), m.iloc[:n]
    joined = pd.concat([p, m], axis=1, join="inner").dropna()
    if len(joined) < 2 or joined.iloc[:, 1].var(ddof=1) == 0:
        return {
            "beta": None, "alpha": None,
            "systematic_return": 0.0, "specific_return": 0.0,
            "systematic_pnl": 0.0, "specific_pnl": 0.0,
            "observations": int(len(joined)),
        }
    pr = joined.iloc[:, 0].to_numpy()
    mr = joined.iloc[:, 1].to_numpy()
    beta = float(np.cov(pr, mr, ddof=1)[0, 1] / np.var(mr, ddof=1))
    alpha = float(pr.mean() - beta * mr.mean())
    systematic = beta * float(mr.mean())
    specific = alpha
    total_return = systematic + specific
    # Split dollar P&L on the return shares (guarding a zero denominator).
    if total_return != 0:
        sys_pnl = total_pnl * (systematic / total_return)
        spec_pnl = total_pnl * (specific / total_return)
    else:
        sys_pnl = spec_pnl = 0.0
    return {
        "beta": round(beta, 4),
        "alpha": round(alpha, 6),
        "systematic_return": round(systematic, 6),
        "specific_return": round(specific, 6),
        "systematic_pnl": round(sys_pnl, 2),
        "specific_pnl": round(spec_pnl, 2),
        "observations": int(len(joined)),
    }


def build_attribution_report(
    data_dir: str,
    starting_capital: float,
    ohlcv_fetcher: Any = None,
    benchmark: str = "SPY",
) -> Dict[str, Any]:
    """Assemble sector + strategy + factor attribution from the trade journal."""
    from pathlib import Path

    from analytics.performance import (
        build_equity_curve,
        load_completed_trades,
    )

    trades = load_completed_trades(Path(data_dir) / "trades.csv")
    total_pnl = 0.0
    if trades is not None and not trades.empty and "pnl_net" in trades.columns:
        total_pnl = float(trades["pnl_net"].sum())

    sectors = sector_attribution(trades)
    strategies = strategy_attribution(trades, starting_capital)

    # Factor attribution: equity-curve daily returns vs the benchmark.
    factor: Dict[str, Any] = {"beta": None, "observations": 0}
    try:
        curve = build_equity_curve(trades, starting_capital)
        equities = [float(pt.get("equity", 0)) for pt in curve if pt.get("equity")]
        port_returns = pd.Series(equities, dtype=float).pct_change().dropna()
        if ohlcv_fetcher is None:
            from data.fetcher import fetch_ohlcv as ohlcv_fetcher  # type: ignore
        market_returns: Optional[pd.Series] = None
        if ohlcv_fetcher is not None and len(port_returns) >= 2:
            from analytics.risk_dashboard import returns_from_ohlcv

            spy_df = ohlcv_fetcher(benchmark)
            mr = returns_from_ohlcv(spy_df) if spy_df is not None else None
            if mr is not None:
                market_returns = mr.tail(len(port_returns)).reset_index(drop=True)
        if market_returns is not None:
            factor = factor_attribution(port_returns, market_returns, total_pnl)
    except Exception:  # noqa: BLE001 -- factor split is best-effort
        factor = {"beta": None, "observations": 0}

    return {
        "total_pnl": round(total_pnl, 2),
        "by_sector": sectors,
        "by_strategy": strategies,
        "factor": factor,
        "benchmark": benchmark,
    }
