"""
Performance analytics computed from the trade journal.

The functions here operate on a *normalised trades DataFrame* -- one row per
completed trade with (at least) these columns:

    pnl_net, pnl_gross, r_multiple, strategy, symbol, entry_time, exit_time

:func:`load_completed_trades` produces such a frame from ``trades.csv``; the
backtester feeds the same shape via :func:`metrics_from_records`, so a live
journal and a backtest are scored by identical maths.

Ratio conventions
-----------------
Sharpe and Sortino are annualised from a *daily* equity curve (252 trading
days/year).  The journal has no intraday marks, so the curve is built by
placing each trade's net P&L on its exit date and forward-filling across
calendar days; this is an approximation but is stable and comparable across
runs.  Drawdown is reported both in dollars and as a fraction of the running
peak equity.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252

# Numeric journal columns coerced on load.
_NUMERIC_COLUMNS = [
    "pnl_net",
    "pnl_gross",
    "pnl_pct",
    "r_multiple",
    "quantity",
    "entry_fill_price",
    "entry_commission",
    "exit_commission",
    "hold_duration_hours",
    "capture_ratio",
    "signal_strength",
]


@dataclass
class PerformanceReport:
    """A full analytics payload for the dashboard / CLI.

    Attributes:
        summary: Portfolio-wide metrics (see :func:`compute_metrics`).
        by_strategy: Per-strategy metric rows.
        by_symbol: Per-symbol metric rows.
        equity_curve: List of ``{"date", "equity"}`` points.
        recent_trades: Most recent completed trades (list of dicts).
    """

    summary: Dict[str, Any] = field(default_factory=dict)
    by_strategy: List[Dict[str, Any]] = field(default_factory=list)
    by_symbol: List[Dict[str, Any]] = field(default_factory=list)
    equity_curve: List[Dict[str, Any]] = field(default_factory=list)
    recent_trades: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary,
            "by_strategy": self.by_strategy,
            "by_symbol": self.by_symbol,
            "equity_curve": self.equity_curve,
            "recent_trades": self.recent_trades,
        }


# ---------------------------------------------------------------------------
# Loading / normalisation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# mtime-guarded cache for the journal CSV (audit B14)
# ---------------------------------------------------------------------------
# The trade journal is read on nearly every dashboard poll (analytics summary,
# by-strategy, by-symbol, equity curve, recent trades, risk report, CSV/PDF
# export).  Re-parsing the whole CSV each time is wasteful when the file hasn't
# changed.  We memoise the parsed frame keyed on (resolved path, mtime_ns,
# size); any write to the file changes mtime/size and invalidates the entry.
# A copy is returned on every hit so callers can never mutate the cached frame.
_CACHE_LOCK = threading.Lock()
_TRADES_CACHE: Dict[str, Tuple[Tuple[int, int], pd.DataFrame]] = {}


def clear_trades_cache() -> None:
    """Drop the memoised journal frames (tests / after a known rewrite)."""
    with _CACHE_LOCK:
        _TRADES_CACHE.clear()


def load_completed_trades(csv_path: str | Path) -> pd.DataFrame:
    """Load completed trades from ``trades.csv`` into a numeric DataFrame.

    A trade is "completed" when it has a non-empty ``exit_time``.  Numeric
    columns are coerced; unparseable values become ``NaN``.

    Returns an empty DataFrame (with no rows) when the file is missing,
    empty, or has no completed trades.  Results are cached per file and reused
    while the file's mtime and size are unchanged (B14).
    """
    path = Path(csv_path)
    key = str(path.resolve() if path.exists() else path)
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None

    if stamp is not None:
        with _CACHE_LOCK:
            cached = _TRADES_CACHE.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1].copy()

    frame = _load_completed_trades_uncached(path)

    if stamp is not None:
        with _CACHE_LOCK:
            _TRADES_CACHE[key] = (stamp, frame)
    return frame.copy()


def _load_completed_trades_uncached(path: Path) -> pd.DataFrame:
    """The actual CSV parse (see :func:`load_completed_trades`)."""
    try:
        df = pd.read_csv(path, dtype=str)
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return pd.DataFrame()
    except pd.errors.ParserError:
        # A row whose field count doesn't match the header (e.g. schema drift
        # between deploys leaving a stale header on disk) would otherwise take
        # down every dashboard page.  Skip the offending rows so the rest of
        # the journal still renders; TradeLogger self-heals the header on its
        # next start.
        df = pd.read_csv(path, dtype=str, on_bad_lines="skip")

    if df.empty or "exit_time" not in df.columns:
        return pd.DataFrame()

    completed = df[df["exit_time"].notna() & (df["exit_time"].astype(str) != "")]
    completed = completed.copy()
    if completed.empty:
        return completed

    for col in _NUMERIC_COLUMNS:
        if col in completed.columns:
            completed[col] = pd.to_numeric(completed[col], errors="coerce")

    return completed.reset_index(drop=True)


def metrics_from_records(
    records: Sequence[Dict[str, Any]],
    starting_capital: float,
) -> "PerformanceReport":
    """Build a :class:`PerformanceReport` from a list of trade dicts.

    Each record needs ``pnl_net``, ``r_multiple``, ``strategy``, ``symbol``,
    and ``exit_time``.  Used by the backtester to reuse the journal maths.
    """
    df = pd.DataFrame(list(records))
    return build_report(df, starting_capital)


# ---------------------------------------------------------------------------
# Individual metrics
# ---------------------------------------------------------------------------


def win_rate(pnl: Sequence[float]) -> float:
    """Fraction of trades with strictly positive P&L (0.0 when no trades)."""
    arr = _clean(pnl)
    if arr.size == 0:
        return 0.0
    return float((arr > 0).sum() / arr.size)


def profit_factor(pnl: Sequence[float]) -> float:
    """Gross profit / gross loss.

    Returns ``inf`` when there are wins but no losses, ``0.0`` when there are
    no wins.
    """
    arr = _clean(pnl)
    gross_profit = float(arr[arr > 0].sum())
    gross_loss = float(-arr[arr < 0].sum())
    if gross_loss == 0.0:
        return math.inf if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def expectancy(pnl: Sequence[float]) -> float:
    """Average net P&L per trade (0.0 when no trades)."""
    arr = _clean(pnl)
    return float(arr.mean()) if arr.size else 0.0


def avg_r_multiple(r: Sequence[float]) -> float:
    """Average R multiple across trades (0.0 when none)."""
    arr = _clean(r)
    return float(arr.mean()) if arr.size else 0.0


def sharpe_ratio(
    returns: Sequence[float],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    risk_free_rate: float = 0.0,
) -> float:
    """Annualised Sharpe ratio of a periodic return series.

    ``mean(excess) / std(excess) * sqrt(periods_per_year)``.  Returns 0.0 when
    there are fewer than two returns or the standard deviation is zero.
    """
    arr = _clean(returns)
    if arr.size < 2:
        return 0.0
    excess = arr - risk_free_rate / periods_per_year
    std = float(excess.std(ddof=1))
    if std == 0.0:
        return 0.0
    return float(excess.mean() / std * math.sqrt(periods_per_year))


def sortino_ratio(
    returns: Sequence[float],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    risk_free_rate: float = 0.0,
) -> float:
    """Annualised Sortino ratio (downside-deviation denominator).

    Returns ``inf`` when there is positive mean excess return and no downside
    volatility, and 0.0 when there are too few returns.
    """
    arr = _clean(returns)
    if arr.size < 2:
        return 0.0
    excess = arr - risk_free_rate / periods_per_year
    downside = excess[excess < 0]
    if downside.size == 0:
        return math.inf if float(excess.mean()) > 0 else 0.0
    downside_dev = math.sqrt(float((downside ** 2).mean()))
    if downside_dev == 0.0:
        return 0.0
    return float(excess.mean() / downside_dev * math.sqrt(periods_per_year))


def max_drawdown(equity: Sequence[float]) -> Dict[str, float]:
    """Return the maximum peak-to-trough drawdown of an equity series.

    Returns ``{"abs": dollars, "pct": fraction_of_peak}`` (both non-negative).
    """
    arr = _clean(equity)
    if arr.size == 0:
        return {"abs": 0.0, "pct": 0.0}
    running_peak = np.maximum.accumulate(arr)
    drawdowns = running_peak - arr
    max_dd_abs = float(drawdowns.max())
    # Percentage drawdown at the point of the largest absolute drawdown.
    idx = int(np.argmax(drawdowns))
    peak_at_idx = float(running_peak[idx])
    max_dd_pct = (max_dd_abs / peak_at_idx) if peak_at_idx > 0 else 0.0
    return {"abs": round(max_dd_abs, 2), "pct": round(max_dd_pct, 4)}


# ---------------------------------------------------------------------------
# Equity curve
# ---------------------------------------------------------------------------


def benchmark_curve(
    dates: List[str],
    start_equity: float,
    closes_by_date: Dict[str, float],
) -> List[Optional[float]]:
    """Align a benchmark price series to *dates*, normalized to *start_equity*.

    For each target date, the most recent benchmark close on or before that
    date is used (so weekend/holiday gaps in the equity curve still map to the
    prior trading day). The series is scaled so its first resolvable point
    equals *start_equity*, making it directly comparable to the equity curve on
    the same axis. Points with no prior close resolve to ``None``.
    """
    if not dates or not closes_by_date or start_equity <= 0:
        return [None for _ in dates]
    sorted_dates = sorted(closes_by_date)
    import bisect

    def _close_on_or_before(target: str) -> Optional[float]:
        idx = bisect.bisect_right(sorted_dates, target) - 1
        if idx < 0:
            return None
        return closes_by_date[sorted_dates[idx]]

    base = _close_on_or_before(dates[0])
    if not base:
        # Fall back to the earliest available close as the normalization base.
        base = closes_by_date[sorted_dates[0]]
    out: List[Optional[float]] = []
    for d in dates:
        c = _close_on_or_before(d)
        out.append(round(start_equity * c / base, 2) if c and base else None)
    return out


def build_equity_curve(
    trades: pd.DataFrame,
    starting_capital: float,
) -> List[Dict[str, Any]]:
    """Build a per-trade cumulative equity curve ordered by exit time.

    Returns a list of ``{"date", "equity", "trade_pnl"}`` points, one per
    completed trade, starting from *starting_capital*.
    """
    if trades.empty or "pnl_net" not in trades.columns:
        return []

    ordered = _order_by_exit(trades)
    equity = float(starting_capital)
    curve: List[Dict[str, Any]] = []
    for _, row in ordered.iterrows():
        pnl = float(row.get("pnl_net", 0.0) or 0.0)
        equity += pnl
        curve.append(
            {
                "date": str(row.get("exit_time", "")),
                "equity": round(equity, 2),
                "trade_pnl": round(pnl, 2),
            }
        )
    return curve


def _daily_equity(trades: pd.DataFrame, starting_capital: float) -> pd.Series:
    """Return a daily-indexed equity Series (for Sharpe/Sortino).

    Trades are grouped by exit *date*; each day's net P&L is added to the
    running equity and the series is forward-filled across the calendar span.
    """
    if trades.empty or "exit_time" not in trades.columns:
        return pd.Series(dtype=float)

    ordered = _order_by_exit(trades)
    exit_dates = pd.to_datetime(ordered["exit_time"], errors="coerce")
    valid = exit_dates.notna()
    if not valid.any():
        return pd.Series(dtype=float)

    pnl = pd.to_numeric(ordered["pnl_net"], errors="coerce").fillna(0.0)[valid]
    by_day = pnl.groupby(exit_dates[valid].dt.normalize()).sum().sort_index()

    equity = float(starting_capital) + by_day.cumsum()
    # Forward-fill across every calendar day so returns reflect elapsed time.
    full_index = pd.date_range(equity.index.min(), equity.index.max(), freq="D")
    equity = equity.reindex(full_index).ffill()
    # Prepend the starting capital so the first day shows a real return.
    start_day = equity.index.min() - pd.Timedelta(days=1)
    equity = pd.concat([pd.Series([float(starting_capital)], index=[start_day]), equity])
    return equity


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def compute_metrics(
    trades: pd.DataFrame,
    starting_capital: float,
) -> Dict[str, Any]:
    """Compute the portfolio-wide metric summary from a trades DataFrame."""
    if trades.empty or "pnl_net" not in trades.columns:
        return _empty_summary(starting_capital)

    pnl = _clean(trades["pnl_net"])
    r = _clean(trades["r_multiple"]) if "r_multiple" in trades.columns else np.array([])
    n = int(pnl.size)
    total_pnl = float(pnl.sum())

    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]

    curve = build_equity_curve(trades, starting_capital)
    equity_values = [starting_capital] + [p["equity"] for p in curve]
    dd = max_drawdown(equity_values)

    daily_eq = _daily_equity(trades, starting_capital)
    daily_returns = daily_eq.pct_change().dropna().to_numpy() if daily_eq.size else np.array([])

    return {
        "starting_capital": round(float(starting_capital), 2),
        "ending_equity": round(float(starting_capital) + total_pnl, 2),
        "total_trades": n,
        "wins": int(wins.size),
        "losses": int(losses.size),
        "win_rate": round(win_rate(pnl), 4),
        "profit_factor": _finite(profit_factor(pnl)),
        "expectancy": round(expectancy(pnl), 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round(
            (total_pnl / starting_capital * 100.0) if starting_capital else 0.0, 2
        ),
        "avg_win": round(float(wins.mean()), 2) if wins.size else 0.0,
        "avg_loss": round(float(losses.mean()), 2) if losses.size else 0.0,
        "largest_win": round(float(wins.max()), 2) if wins.size else 0.0,
        "largest_loss": round(float(losses.min()), 2) if losses.size else 0.0,
        "avg_r_multiple": round(avg_r_multiple(r), 3),
        "sharpe_ratio": round(sharpe_ratio(daily_returns), 3),
        "sortino_ratio": _finite(sortino_ratio(daily_returns), digits=3),
        "max_drawdown_abs": dd["abs"],
        "max_drawdown_pct": dd["pct"],
    }


def breakdown_by(
    trades: pd.DataFrame,
    column: str,
    starting_capital: float,
) -> List[Dict[str, Any]]:
    """Return per-group metric rows grouped by *column* (strategy / symbol).

    Each group is scored on its own with a notional starting capital so
    win-rate, profit factor, expectancy, and totals are comparable.
    """
    if trades.empty or column not in trades.columns:
        return []

    rows: List[Dict[str, Any]] = []
    for key, group in trades.groupby(column):
        pnl = _clean(group["pnl_net"])
        r = _clean(group["r_multiple"]) if "r_multiple" in group.columns else np.array([])
        wins = pnl[pnl > 0]
        losses = pnl[pnl < 0]
        rows.append(
            {
                column: str(key),
                "trades": int(pnl.size),
                "wins": int(wins.size),
                "losses": int(losses.size),
                "win_rate": round(win_rate(pnl), 4),
                "profit_factor": _finite(profit_factor(pnl)),
                "expectancy": round(expectancy(pnl), 2),
                "avg_win": round(float(wins.mean()), 2) if wins.size else 0.0,
                "avg_loss": round(float(losses.mean()), 2) if losses.size else 0.0,
                "total_pnl": round(float(pnl.sum()), 2),
                "avg_r_multiple": round(avg_r_multiple(r), 3),
            }
        )
    rows.sort(key=lambda x: x["total_pnl"], reverse=True)
    return rows


def build_report(
    trades: pd.DataFrame,
    starting_capital: float,
    recent_n: int = 20,
) -> PerformanceReport:
    """Assemble a full :class:`PerformanceReport` from a trades DataFrame."""
    summary = compute_metrics(trades, starting_capital)
    by_strategy = breakdown_by(trades, "strategy", starting_capital)
    by_symbol = breakdown_by(trades, "symbol", starting_capital)
    equity_curve = build_equity_curve(trades, starting_capital)

    recent: List[Dict[str, Any]] = []
    if not trades.empty:
        ordered = _order_by_exit(trades).tail(recent_n)
        recent = ordered.replace({np.nan: None}).to_dict(orient="records")

    return PerformanceReport(
        summary=summary,
        by_strategy=by_strategy,
        by_symbol=by_symbol,
        equity_curve=equity_curve,
        recent_trades=recent,
    )


def analyze_journal(
    csv_path: str | Path,
    starting_capital: float,
    recent_n: int = 20,
) -> PerformanceReport:
    """Load ``trades.csv`` and return a full :class:`PerformanceReport`."""
    trades = load_completed_trades(csv_path)
    return build_report(trades, starting_capital, recent_n=recent_n)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _clean(values: Sequence[float]) -> np.ndarray:
    """Coerce to a float array, dropping NaN/inf."""
    arr = pd.to_numeric(pd.Series(list(values)), errors="coerce").to_numpy(dtype=float)
    return arr[np.isfinite(arr)]


def _order_by_exit(trades: pd.DataFrame) -> pd.DataFrame:
    """Return *trades* ordered by exit time (stable; unparseable last)."""
    if "exit_time" not in trades.columns:
        return trades
    ordered = trades.copy()
    ordered["_exit_dt"] = pd.to_datetime(ordered["exit_time"], errors="coerce")
    ordered = ordered.sort_values("_exit_dt", kind="stable", na_position="last")
    return ordered.drop(columns="_exit_dt")


def _finite(value: float, digits: int = 3) -> Optional[float]:
    """Round a metric, mapping non-finite values (inf) to ``None`` for JSON."""
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)


def _empty_summary(starting_capital: float) -> Dict[str, Any]:
    """Return an all-zero summary for the no-trades case."""
    return {
        "starting_capital": round(float(starting_capital), 2),
        "ending_equity": round(float(starting_capital), 2),
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "profit_factor": None,
        "expectancy": 0.0,
        "total_pnl": 0.0,
        "total_return_pct": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "largest_win": 0.0,
        "largest_loss": 0.0,
        "avg_r_multiple": 0.0,
        "sharpe_ratio": 0.0,
        "sortino_ratio": 0.0,
        "max_drawdown_abs": 0.0,
        "max_drawdown_pct": 0.0,
    }
