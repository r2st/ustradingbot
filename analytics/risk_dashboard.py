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
import structlog

from analytics.performance import (
    build_equity_curve,
    load_completed_trades,
    max_drawdown,
)
from config.settings import EASTERN
from config.universe import get_sector

log = structlog.get_logger(__name__)


@dataclass
class RiskReport:
    """Full portfolio risk payload for the dashboard."""

    exposure: Dict[str, Any] = field(default_factory=dict)
    sector_concentration: List[Dict[str, Any]] = field(default_factory=list)
    correlations: List[Dict[str, Any]] = field(default_factory=list)
    max_correlation: Optional[Dict[str, Any]] = None
    drawdown: Dict[str, Any] = field(default_factory=dict)
    pnl_breakdown: Dict[str, Any] = field(default_factory=dict)
    # Monitoring F5 additions (all additive — existing consumers unaffected).
    open_risk: Dict[str, Any] = field(default_factory=dict)
    daily_loss_budget: Dict[str, Any] = field(default_factory=dict)
    marked_to_market: bool = False
    # P3f — portfolio beta vs SPY + market-relative drawdown (additive).
    beta: Dict[str, Any] = field(default_factory=dict)
    # P0-1 — portfolio VaR / CVaR (parametric + historical), additive.
    var_cvar: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exposure": self.exposure,
            "sector_concentration": self.sector_concentration,
            "correlations": self.correlations,
            "max_correlation": self.max_correlation,
            "drawdown": self.drawdown,
            "pnl_breakdown": self.pnl_breakdown,
            "open_risk": self.open_risk,
            "daily_loss_budget": self.daily_loss_budget,
            "marked_to_market": self.marked_to_market,
            "beta": self.beta,
            "var_cvar": self.var_cvar,
        }


# ---------------------------------------------------------------------------
# Exposure
# ---------------------------------------------------------------------------


def portfolio_exposure(
    positions: Sequence[Dict[str, Any]],
    capital_by_currency: Dict[str, float],
    prices: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Return committed / available capital per currency and overall.

    Cost-basis exposure for a position is ``entry_price * quantity`` in its
    own currency.  When *prices* supplies a current price for the symbol, a
    ``market_value`` is reported alongside (falling back to cost basis for
    symbols without a quote, so the totals never understate the book).
    """
    prices = prices or {}
    by_currency: Dict[str, Dict[str, float]] = {}
    for cur, allocated in capital_by_currency.items():
        by_currency[cur.upper()] = {
            "allocated": round(float(allocated), 2),
            "committed": 0.0,
            "market_value": 0.0,
        }

    for pos in positions:
        cur = str(pos.get("currency", "USD")).upper()
        try:
            entry = float(pos.get("entry_price", 0) or 0)
            qty = int(float(pos.get("quantity", 0) or 0))
        except (ValueError, TypeError):
            continue
        cost = entry * qty
        current = prices.get(str(pos.get("symbol", "")))
        mv = (float(current) * qty) if current else cost
        bucket = by_currency.setdefault(
            cur, {"allocated": 0.0, "committed": 0.0, "market_value": 0.0}
        )
        bucket["committed"] += cost
        bucket["market_value"] += mv

    rows: List[Dict[str, Any]] = []
    total_alloc = 0.0
    total_committed = 0.0
    total_mv = 0.0
    for cur, b in by_currency.items():
        alloc = b["allocated"]
        committed = round(b["committed"], 2)
        mv = round(b["market_value"], 2)
        total_alloc += alloc
        total_committed += committed
        total_mv += mv
        rows.append(
            {
                "currency": cur,
                "allocated": alloc,
                "committed": committed,
                "market_value": mv,
                "available": round(alloc - committed, 2),
                "exposure_pct": round(committed / alloc, 4) if alloc > 0 else 0.0,
            }
        )
    rows.sort(key=lambda r: r["currency"])
    return {
        "by_currency": rows,
        "total_allocated": round(total_alloc, 2),
        "total_committed": round(total_committed, 2),
        "total_market_value": round(total_mv, 2),
        "gross_exposure_pct": (
            round(total_committed / total_alloc, 4) if total_alloc > 0 else 0.0
        ),
        "open_positions": len(positions),
    }


# ---------------------------------------------------------------------------
# Open risk (F5) — dollars lost if every stop is hit right now
# ---------------------------------------------------------------------------


def open_risk(
    positions: Sequence[Dict[str, Any]],
    prices: Optional[Dict[str, float]] = None,
    total_capital: float = 0.0,
) -> Dict[str, Any]:
    """Return per-position and aggregate risk-to-stop.

    For a long, ``risk_if_stopped = (current − stop) × quantity`` — the
    amount lost if the stop is hit from here (current falls back to entry
    when no quote is available).  A stop trailed *above* the mark yields a
    **negative** number: locked-in profit rather than risk.  Shorts are
    sign-mirrored.  The aggregate sums positive risks only (``total``) and
    also reports the net including locked profits (``net``).
    """
    prices = prices or {}
    rows: List[Dict[str, Any]] = []
    total_at_risk = 0.0
    net = 0.0
    for pos in positions:
        symbol = str(pos.get("symbol", ""))
        try:
            entry = float(pos.get("entry_price", 0) or 0)
            stop = float(pos.get("stop_price", 0) or 0)
            qty = int(float(pos.get("quantity", 0) or 0))
        except (ValueError, TypeError):
            continue
        current = prices.get(symbol)
        mark = float(current) if current else entry
        side = str(pos.get("direction", "long") or "long").lower()
        if side == "short":
            risk = (stop - mark) * qty
        else:
            risk = (mark - stop) * qty
        risk = round(risk, 2)
        net += risk
        if risk > 0:
            total_at_risk += risk
        rows.append(
            {
                "symbol": symbol,
                "risk_if_stopped": risk,
                "locked_profit": risk < 0,
                "pct_of_capital": (
                    round(risk / total_capital, 4) if total_capital > 0 else 0.0
                ),
                "marked": bool(current),
            }
        )
    rows.sort(key=lambda r: r["risk_if_stopped"], reverse=True)
    return {
        "per_position": rows,
        "total": round(total_at_risk, 2),
        "net": round(net, 2),
        "pct_of_capital": (
            round(total_at_risk / total_capital, 4) if total_capital > 0 else 0.0
        ),
    }


def daily_loss_budget(
    today_pnl: float,
    total_capital: float,
    limit_pct: float,
) -> Dict[str, Any]:
    """Return today's loss-budget usage against ``DAILY_LOSS_LIMIT_PCT``."""
    limit_usd = round(float(total_capital) * float(limit_pct), 2)
    used = round(max(0.0, -float(today_pnl)), 2)
    return {
        "limit_pct": float(limit_pct),
        "limit_usd": limit_usd,
        "used_today": used,
        "remaining": round(max(0.0, limit_usd - used), 2),
        "used_pct_of_budget": round(used / limit_usd, 4) if limit_usd > 0 else 0.0,
        "today_pnl": round(float(today_pnl), 2),
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
# Portfolio beta vs SPY (P3f)
# ---------------------------------------------------------------------------


def portfolio_beta(
    returns_by_symbol: Dict[str, pd.Series],
    weights: Dict[str, float],
    market_returns: Optional[pd.Series],
    min_overlap: int = 20,
    benchmark: str = "SPY",
) -> Dict[str, Any]:
    """Portfolio beta vs the market (SPY) from aligned daily returns.

    Each position's beta is ``cov(asset, market) / var(market)`` over the
    overlapping window; the portfolio beta is the market-value-weighted sum of
    the per-position betas.  Positions with fewer than *min_overlap* overlapping
    observations (or where the market has zero variance) get a ``None`` beta and
    are excluded from the weighted sum, with the remaining weights renormalised
    so the portfolio beta always reflects the covered book.
    """
    empty = {
        "portfolio_beta": None,
        "per_position": [],
        "observations": 0,
        "benchmark": benchmark,
    }
    if market_returns is None or len(market_returns) < min_overlap:
        return empty
    if float(market_returns.std()) == 0.0:
        return empty

    total_weight = sum(max(0.0, float(w)) for w in weights.values())
    per_position: List[Dict[str, Any]] = []
    covered: List[tuple] = []  # (symbol, beta, raw_weight)
    max_obs = 0

    for symbol, series in returns_by_symbol.items():
        raw_w = max(0.0, float(weights.get(symbol, 0.0)))
        weight = round(raw_w / total_weight, 4) if total_weight > 0 else 0.0
        joined = pd.concat([series, market_returns], axis=1, join="inner").dropna()
        beta: Optional[float] = None
        if len(joined) >= min_overlap:
            asset = joined.iloc[:, 0].to_numpy()
            market = joined.iloc[:, 1].to_numpy()
            # Compute cov and var with the same (population) normalisation so the
            # ratio is exact — mixing np.cov (ddof=1) with np.var (ddof=0) would
            # bias beta by N/(N-1).
            m_mean = float(np.mean(market))
            var = float(np.mean((market - m_mean) ** 2))
            if var > 0:
                cov = float(np.mean((asset - np.mean(asset)) * (market - m_mean)))
                b = cov / var
                if not np.isnan(b):
                    beta = round(b, 3)
                    max_obs = max(max_obs, int(len(joined)))
        per_position.append({"symbol": symbol, "beta": beta, "weight": weight})
        if beta is not None and raw_w > 0:
            covered.append((symbol, beta, raw_w))

    covered_weight = sum(w for _, _, w in covered)
    if covered_weight > 0:
        port_beta = round(
            sum(beta * (w / covered_weight) for _, beta, w in covered), 3
        )
    else:
        port_beta = None

    per_position.sort(key=lambda r: (r["beta"] is None, -(r["weight"] or 0.0)))
    return {
        "portfolio_beta": port_beta,
        "per_position": per_position,
        "observations": max_obs,
        "benchmark": benchmark,
    }


def market_relative_drawdown(
    portfolio_equities: Sequence[float],
    benchmark_equities: Sequence[float],
) -> Dict[str, Any]:
    """Current drawdown of the book vs a benchmark buy-&-hold over the same span.

    ``excess_drawdown_pct`` is the portfolio's current drawdown minus the
    benchmark's — positive means the book is deeper underwater than the market.
    Robust to short/empty inputs (fields fall back to ``0.0``).
    """
    def _current_dd(equities: Sequence[float]) -> float:
        vals = [float(v) for v in equities if v is not None]
        if len(vals) < 1:
            return 0.0
        peak = max(vals)
        if peak <= 0:
            return 0.0
        return round(max(0.0, (peak - vals[-1]) / peak), 4)

    port_dd = _current_dd(portfolio_equities)
    bench_dd = _current_dd(benchmark_equities)
    return {
        "portfolio_drawdown_pct": port_dd,
        "benchmark_drawdown_pct": bench_dd,
        "excess_drawdown_pct": round(port_dd - bench_dd, 4),
    }


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
    now = now or datetime.now(tz=EASTERN)
    empty = {
        "today": 0.0, "week": 0.0, "month": 0.0,
        "daily": [], "weekly": [], "monthly": [],
    }
    if trades.empty or "exit_time" not in trades.columns or "pnl_net" not in trades:
        return empty

    exits = pd.to_datetime(trades["exit_time"], errors="coerce")
    # Persisted exit_time values are tz-naive Eastern wall-clock; make the
    # series tz-aware so comparisons against the tz-aware ``now`` boundaries
    # below don't raise "Invalid comparison between dtype=datetime64 and
    # Timestamp".
    if getattr(exits.dt, "tz", None) is None:
        exits = exits.dt.tz_localize(
            EASTERN, ambiguous="NaT", nonexistent="shift_forward"
        )
    else:
        exits = exits.dt.tz_convert(EASTERN)
    pnl = pd.to_numeric(trades["pnl_net"], errors="coerce").fillna(0.0)
    valid = exits.notna()
    if not valid.any():
        return empty
    df = pd.DataFrame({"exit": exits[valid], "pnl": pnl[valid]}).set_index("exit")

    ts = pd.Timestamp(now)
    if ts.tzinfo is None:
        ts = ts.tz_localize(EASTERN)
    else:
        ts = ts.tz_convert(EASTERN)
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
    prices: Optional[Dict[str, float]] = None,
    daily_loss_limit_pct: Optional[float] = None,
    var_confidence: float = 0.95,
    var_horizon_days: int = 1,
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
        prices: Optional ``{symbol: current_price}`` map (from the dashboard
            quote service).  When present, exposure gains market values and
            open risk is marked to market; when absent everything is valued
            at cost so the report never errors (F5).
        daily_loss_limit_pct: ``Settings.DAILY_LOSS_LIMIT_PCT``; enables the
            daily-loss-budget block when provided.
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
    returns: Dict[str, pd.Series] = {}
    if ohlcv_fetcher is not None and len(positions) >= 1:
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
        if len(positions) >= 2:
            correlations = position_correlations(returns)

    # P3f — portfolio beta vs SPY (best-effort; never breaks the report).
    beta: Dict[str, Any] = {"portfolio_beta": None, "per_position": [],
                            "observations": 0, "benchmark": "SPY"}
    if ohlcv_fetcher is not None and returns:
        try:
            spy_df = ohlcv_fetcher("SPY")
            market_returns = returns_from_ohlcv(spy_df) if spy_df is not None else None
            weights: Dict[str, float] = {}
            for pos in positions:
                symbol = str(pos.get("symbol", ""))
                if not symbol:
                    continue
                try:
                    qty = int(float(pos.get("quantity", 0) or 0))
                    px = prices.get(symbol) if prices else None
                    mark = float(px) if px else float(pos.get("entry_price", 0) or 0)
                except (ValueError, TypeError):
                    continue
                weights[symbol] = weights.get(symbol, 0.0) + mark * qty
            beta = portfolio_beta(returns, weights, market_returns)
        except Exception as exc:  # noqa: BLE001 -- additive; degrade gracefully
            log.warning("risk.beta_failed", error=str(exc),
                        error_type=type(exc).__name__)

    # P0-1 — portfolio VaR / CVaR from the same aligned returns (best-effort).
    var_cvar: Dict[str, Any] = {}
    if returns:
        try:
            from risk.limits import portfolio_var_cvar

            var_weights: Dict[str, float] = {}
            for pos in positions:
                symbol = str(pos.get("symbol", ""))
                if not symbol:
                    continue
                try:
                    qty = int(float(pos.get("quantity", 0) or 0))
                    px = prices.get(symbol) if prices else None
                    mark = float(px) if px else float(pos.get("entry_price", 0) or 0)
                except (ValueError, TypeError):
                    continue
                var_weights[symbol] = var_weights.get(symbol, 0.0) + mark * qty
            var_cvar = portfolio_var_cvar(
                returns, var_weights, var_confidence, var_horizon_days
            )
        except Exception as exc:  # noqa: BLE001 -- additive; degrade gracefully
            log.warning("risk.var_failed", error=str(exc),
                        error_type=type(exc).__name__)

    breakdown = pnl_breakdown(trades, now=now)
    marked = bool(prices) and any(
        prices.get(str(p.get("symbol", ""))) for p in positions
    )
    budget: Dict[str, Any] = {}
    if daily_loss_limit_pct is not None:
        budget = daily_loss_budget(
            breakdown.get("today", 0.0), starting_capital, daily_loss_limit_pct
        )

    report = RiskReport(
        exposure=portfolio_exposure(positions, capital_by_currency, prices=prices),
        sector_concentration=sector_concentration(positions),
        correlations=correlations,
        max_correlation=correlations[0] if correlations else None,
        drawdown=drawdown_tracking(trades, starting_capital),
        pnl_breakdown=breakdown,
        open_risk=open_risk(positions, prices=prices,
                            total_capital=starting_capital),
        daily_loss_budget=budget,
        marked_to_market=marked,
        beta=beta,
        var_cvar=var_cvar,
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
