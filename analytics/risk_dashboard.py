"""
Real-time portfolio risk analytics for the dashboard.

Builds a :class:`RiskReport` from the open-position book and the trade journal:

* **Exposure** — committed capital and gross exposure per currency and overall,
  as a fraction of allocated capital.
* **Sector concentration** — how the open book is distributed across sectors
  (via :data:`config.universe.SECTOR_BY_SYMBOL`), with the single largest
  concentration flagged.
* **Correlation** — pairwise Pearson correlation of recent daily returns
  between open positions (a book of highly-correlated names is really one bet).
* **Drawdown** — current and maximum peak-to-trough equity drawdown.
* **P&L breakdown** — realised P&L for today, this week, this month, and the
  trailing daily / weekly / monthly series.

The heavy-lifting functions accept plain data (a positions list, a trades
DataFrame, a ``{symbol: returns}`` mapping) so they unit-test without any
network or filesystem access; :func:`build_risk_report` wires them to the live
data sources.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from analytics.performance import (
    build_equity_curve,
    load_completed_trades,
    max_drawdown,
)
from config.universe import get_sector


@dataclass
class RiskReport:
    """Full portfolio risk payload for the dashboard."""

    exposure: Dict[str, Any] = field(default_factory=dict)
    sector_concentration: List[Dict[str, Any]] = field(default_factory=list)
    correlations: List[Dict[str, Any]] = field(default_factory=list)
    max_correlation: Optional[Dict[str, Any]] = None
    drawdown: Dict[str, Any] = field(default_factory=dict)
    pnl_breakdown: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exposure": self.exposure,
            "sector_concentration": self.sector_concentration,
            "correlations": self.correlations,
            "max_correlation": self.max_correlation,
            "drawdown": self.drawdown,
            "pnl_breakdown": self.pnl_breakdown,
        }


# ---------------------------------------------------------------------------
# Exposure
# ---------------------------------------------------------------------------


def portfolio_exposure(
    positions: Sequence[Dict[str, Any]],
    capital_by_currency: Dict[str, float],
) -> Dict[str, Any]:
    """Return committed / available capital per currency and overall.

    Exposure for a position is ``entry_price * quantity`` in its own currency.
    """
    by_currency: Dict[str, Dict[str, float]] = {}
    for cur, allocated in capital_by_currency.items():
        by_currency[cur.upper()] = {
            "allocated": round(float(allocated), 2),
            "committed": 0.0,
        }

    for pos in positions:
        cur = str(pos.get("currency", "USD")).upper()
        try:
            cost = float(pos.get("entry_price", 0) or 0) * int(
                float(pos.get("quantity", 0) or 0)
            )
        except (ValueError, TypeError):
            continue
        bucket = by_currency.setdefault(cur, {"allocated": 0.0, "committed": 0.0})
        bucket["committed"] += cost

    rows: List[Dict[str, Any]] = []
    total_alloc = 0.0
    total_committed = 0.0
    for cur, b in by_currency.items():
        alloc = b["allocated"]
        committed = round(b["committed"], 2)
        total_alloc += alloc
        total_committed += committed
        rows.append(
            {
                "currency": cur,
                "allocated": alloc,
                "committed": committed,
                "available": round(alloc - committed, 2),
                "exposure_pct": round(committed / alloc, 4) if alloc > 0 else 0.0,
            }
        )
    rows.sort(key=lambda r: r["currency"])
    return {
        "by_currency": rows,
        "total_allocated": round(total_alloc, 2),
        "total_committed": round(total_committed, 2),
        "gross_exposure_pct": (
            round(total_committed / total_alloc, 4) if total_alloc > 0 else 0.0
        ),
        "open_positions": len(positions),
    }


# ---------------------------------------------------------------------------
# Sector concentration
# ---------------------------------------------------------------------------


def sector_concentration(
    positions: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return per-sector exposure of the open book, largest first."""
    totals: Dict[str, Dict[str, float]] = {}
    grand_total = 0.0
    for pos in positions:
        symbol = str(pos.get("symbol", ""))
        sector = get_sector(symbol)
        try:
            cost = float(pos.get("entry_price", 0) or 0) * int(
                float(pos.get("quantity", 0) or 0)
            )
        except (ValueError, TypeError):
            continue
        bucket = totals.setdefault(sector, {"exposure": 0.0, "count": 0})
        bucket["exposure"] += cost
        bucket["count"] += 1
        grand_total += cost

    rows: List[Dict[str, Any]] = []
    for sector, b in totals.items():
        rows.append(
            {
                "sector": sector,
                "positions": int(b["count"]),
                "exposure": round(b["exposure"], 2),
                "pct": round(b["exposure"] / grand_total, 4) if grand_total > 0 else 0.0,
            }
        )
    rows.sort(key=lambda r: r["exposure"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


def position_correlations(
    returns_by_symbol: Dict[str, pd.Series],
    min_overlap: int = 20,
) -> List[Dict[str, Any]]:
    """Return pairwise Pearson correlations of aligned daily returns.

    Each pair needs at least *min_overlap* overlapping observations to be
    reported; pairs with insufficient history are skipped.  Results are sorted
    by descending absolute correlation (the most dangerous pairs first).
    """
    symbols = sorted(returns_by_symbol.keys())
    out: List[Dict[str, Any]] = []
    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            a, b = symbols[i], symbols[j]
            sa, sb = returns_by_symbol[a], returns_by_symbol[b]
            joined = pd.concat([sa, sb], axis=1, join="inner").dropna()
            if len(joined) < min_overlap:
                continue
            x = joined.iloc[:, 0].to_numpy()
            y = joined.iloc[:, 1].to_numpy()
            if x.std() == 0 or y.std() == 0:
                continue
            corr = float(np.corrcoef(x, y)[0, 1])
            if np.isnan(corr):
                continue
            out.append({"a": a, "b": b, "correlation": round(corr, 3),
                        "observations": int(len(joined))})
    out.sort(key=lambda r: abs(r["correlation"]), reverse=True)
    return out


def returns_from_ohlcv(df: pd.DataFrame, lookback: int = 60) -> Optional[pd.Series]:
    """Return the trailing daily-return series from an OHLCV frame."""
    if df is None or "Close" not in df or len(df) < 2:
        return None
    close = df["Close"].astype(float).tail(lookback + 1)
    return close.pct_change().dropna()


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------


def drawdown_tracking(
    trades: pd.DataFrame,
    starting_capital: float,
) -> Dict[str, Any]:
    """Return current and maximum drawdown from the journal equity curve."""
    curve = build_equity_curve(trades, starting_capital)
    equities = [float(starting_capital)] + [p["equity"] for p in curve]
    dd = max_drawdown(equities)
    peak = max(equities) if equities else float(starting_capital)
    current_equity = equities[-1] if equities else float(starting_capital)
    current_dd_abs = max(0.0, peak - current_equity)
    current_dd_pct = (current_dd_abs / peak) if peak > 0 else 0.0
    return {
        "current_equity": round(current_equity, 2),
        "peak_equity": round(peak, 2),
        "current_drawdown_abs": round(current_dd_abs, 2),
        "current_drawdown_pct": round(current_dd_pct, 4),
        "max_drawdown_abs": dd["abs"],
        "max_drawdown_pct": dd["pct"],
    }


# ---------------------------------------------------------------------------
# P&L breakdown
# ---------------------------------------------------------------------------


def pnl_breakdown(
    trades: pd.DataFrame,
    now: Optional[datetime] = None,
    recent_days: int = 30,
    recent_weeks: int = 12,
    recent_months: int = 12,
) -> Dict[str, Any]:
    """Return realised P&L for today / this week / this month plus recent series."""
    now = now or datetime.now()
    empty = {
        "today": 0.0, "week": 0.0, "month": 0.0,
        "daily": [], "weekly": [], "monthly": [],
    }
    if trades.empty or "exit_time" not in trades.columns or "pnl_net" not in trades:
        return empty

    exits = pd.to_datetime(trades["exit_time"], errors="coerce")
    pnl = pd.to_numeric(trades["pnl_net"], errors="coerce").fillna(0.0)
    valid = exits.notna()
    if not valid.any():
        return empty
    df = pd.DataFrame({"exit": exits[valid], "pnl": pnl[valid]}).set_index("exit")

    ts = pd.Timestamp(now)
    today = ts.normalize()
    week_start = today - pd.Timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    def _sum_since(start: pd.Timestamp) -> float:
        return round(float(df.loc[df.index >= start, "pnl"].sum()), 2)

    daily = _series(df, "D", recent_days)
    weekly = _series(df, "W", recent_weeks)
    monthly = _series(df, "MS", recent_months)

    return {
        "today": _sum_since(today),
        "week": _sum_since(week_start),
        "month": _sum_since(month_start),
        "daily": daily,
        "weekly": weekly,
        "monthly": monthly,
    }


def _series(df: pd.DataFrame, freq: str, tail: int) -> List[Dict[str, Any]]:
    """Resample net P&L to *freq* and return the last *tail* buckets."""
    grouped = df["pnl"].resample(freq).sum()
    grouped = grouped.tail(tail)
    return [
        {"period": idx.strftime("%Y-%m-%d"), "pnl": round(float(val), 2)}
        for idx, val in grouped.items()
    ]


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------


def build_risk_report(
    data_dir: str | Path,
    capital_by_currency: Dict[str, float],
    starting_capital: float,
    ohlcv_fetcher: Any = None,
    now: Optional[datetime] = None,
) -> RiskReport:
    """Assemble a full :class:`RiskReport` from the live data sources.

    Args:
        data_dir: Directory holding ``open_positions.json`` and ``trades.csv``.
        capital_by_currency: Allocated capital per currency.
        starting_capital: Total starting capital for drawdown/equity.
        ohlcv_fetcher: Callable ``symbol -> OHLCV DataFrame`` used to build the
            correlation matrix.  Defaults to ``data.fetcher.fetch_ohlcv``;
            failures per-symbol are ignored so a data outage degrades the
            correlation section gracefully rather than erroring the page.
    """
    data_dir = Path(data_dir)
    positions = _load_positions(data_dir)
    trades = load_completed_trades(data_dir / "trades.csv")

    correlations: List[Dict[str, Any]] = []
    if ohlcv_fetcher is None:
        try:
            from data.fetcher import fetch_ohlcv as ohlcv_fetcher  # type: ignore
        except Exception:  # noqa: BLE001
            ohlcv_fetcher = None
    if ohlcv_fetcher is not None and len(positions) >= 2:
        returns: Dict[str, pd.Series] = {}
        for pos in positions:
            symbol = str(pos.get("symbol", ""))
            if not symbol:
                continue
            try:
                df = ohlcv_fetcher(symbol)
                series = returns_from_ohlcv(df) if df is not None else None
            except Exception:  # noqa: BLE001
                series = None
            if series is not None and not series.empty:
                returns[symbol] = series
        correlations = position_correlations(returns)

    report = RiskReport(
        exposure=portfolio_exposure(positions, capital_by_currency),
        sector_concentration=sector_concentration(positions),
        correlations=correlations,
        max_correlation=correlations[0] if correlations else None,
        drawdown=drawdown_tracking(trades, starting_capital),
        pnl_breakdown=pnl_breakdown(trades, now=now),
    )
    return report


def _load_positions(data_dir: Path) -> List[Dict[str, Any]]:
    import json

    path = data_dir / "open_positions.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return list(data.values()) if isinstance(data, dict) else []
