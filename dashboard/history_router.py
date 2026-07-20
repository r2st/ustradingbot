"""
Trade history & analytics API (monitoring feature 2).

Server-side pagination, filtering, and sorting over the full ``trades.csv``
journal (never ships the whole CSV to the browser), plus derived stats
(hold times, best/worst trades, streaks) and a rolling win-rate trend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends

from dashboard.auth import get_settings, require_auth

router = APIRouter(prefix="/api/history", tags=["Analytics"])

_SORTABLE = {
    "exit_time", "entry_time", "symbol", "strategy", "pnl_net", "pnl_pct",
    "r_multiple", "hold_duration_hours", "grade",
}

_NUMERIC_SORT = {"pnl_net", "pnl_pct", "r_multiple", "hold_duration_hours"}


def _trades_df() -> pd.DataFrame:
    from analytics.performance import load_completed_trades

    settings = get_settings()
    return load_completed_trades(Path(settings.DATA_DIR) / "trades.csv")


def _clean_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """DataFrame → JSON-safe dicts (NaN → None)."""
    return df.replace({np.nan: None}).to_dict(orient="records")


@router.get("/trades")
async def history_trades(
    limit: int = 50,
    offset: int = 0,
    strategy: str = "",
    symbol: str = "",
    exit_reason: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "exit_time",
    order: str = "desc",
    _user: str = Depends(require_auth),
):
    """Paginated, filterable view over the completed-trade journal."""
    df = _trades_df()
    if df.empty:
        return {"total": 0, "trades": []}

    if strategy and "strategy" in df.columns:
        df = df[df["strategy"].astype(str) == strategy]
    if symbol and "symbol" in df.columns:
        df = df[df["symbol"].astype(str).str.upper() == symbol.upper()]
    if exit_reason and "exit_reason" in df.columns:
        df = df[df["exit_reason"].astype(str) == exit_reason]
    if (date_from or date_to) and "exit_time" in df.columns:
        exits = pd.to_datetime(df["exit_time"], errors="coerce")
        if date_from:
            start = pd.to_datetime(date_from, errors="coerce")
            if pd.notna(start):
                df = df[exits >= start]
                exits = exits[exits >= start]
        if date_to:
            end = pd.to_datetime(date_to, errors="coerce")
            if pd.notna(end):
                end = end + pd.Timedelta(days=1)  # inclusive end date
                df = df[exits < end]

    total = int(len(df))
    sort_col = sort if sort in _SORTABLE and sort in df.columns else "exit_time"
    ascending = str(order).lower() == "asc"
    if sort_col in _NUMERIC_SORT:
        keys = pd.to_numeric(df[sort_col], errors="coerce")
    elif sort_col in ("exit_time", "entry_time"):
        keys = pd.to_datetime(df[sort_col], errors="coerce")
    else:
        keys = df[sort_col].astype(str)
    df = df.assign(_k=keys).sort_values(
        "_k", ascending=ascending, kind="stable", na_position="last"
    ).drop(columns="_k")

    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    page = df.iloc[offset: offset + limit]
    return {"total": total, "trades": _clean_records(page)}


@router.get("/filters")
async def history_filters(_user: str = Depends(require_auth)):
    """Distinct strategies / exit reasons for the filter dropdowns."""
    df = _trades_df()
    if df.empty:
        return {"strategies": [], "exit_reasons": []}
    return {
        "strategies": sorted(df.get("strategy", pd.Series(dtype=str))
                             .dropna().astype(str).unique().tolist()),
        "exit_reasons": sorted(df.get("exit_reason", pd.Series(dtype=str))
                               .dropna().astype(str).unique().tolist()),
    }


@router.get("/stats")
async def history_stats(_user: str = Depends(require_auth)):
    """Derived stats: hold times, best/worst trade, streaks, exit-reason mix."""
    df = _trades_df()
    if df.empty or "pnl_net" not in df.columns:
        return {
            "avg_hold_hours": None, "median_hold_hours": None,
            "best": None, "worst": None,
            "longest_win_streak": 0, "longest_loss_streak": 0,
            "current_streak": 0, "by_exit_reason": {},
        }

    hold = pd.to_numeric(df.get("hold_duration_hours"), errors="coerce").dropna()
    pnl = pd.to_numeric(df["pnl_net"], errors="coerce")

    best = worst = None
    valid = df[pnl.notna()]
    if not valid.empty:
        vp = pnl[pnl.notna()]
        best = _clean_records(valid.loc[[vp.idxmax()]])[0]
        worst = _clean_records(valid.loc[[vp.idxmin()]])[0]

    # Streaks over trades ordered by exit time.
    ordered = df.copy()
    ordered["_exit"] = pd.to_datetime(ordered.get("exit_time"), errors="coerce")
    ordered = ordered.sort_values("_exit", kind="stable")
    seq = pd.to_numeric(ordered["pnl_net"], errors="coerce").dropna()
    longest_win = longest_loss = cur = 0
    for v in seq:
        if v > 0:
            cur = cur + 1 if cur > 0 else 1
        elif v < 0:
            cur = cur - 1 if cur < 0 else -1
        else:
            cur = 0
        longest_win = max(longest_win, cur)
        longest_loss = min(longest_loss, cur)

    by_reason: Dict[str, Dict[str, Any]] = {}
    if "exit_reason" in df.columns:
        for reason, group in df.groupby(df["exit_reason"].astype(str)):
            gp = pd.to_numeric(group["pnl_net"], errors="coerce").dropna()
            by_reason[str(reason)] = {
                "count": int(len(group)),
                "total_pnl": round(float(gp.sum()), 2) if gp.size else 0.0,
            }

    return {
        "avg_hold_hours": round(float(hold.mean()), 1) if hold.size else None,
        "median_hold_hours": round(float(hold.median()), 1) if hold.size else None,
        "best": best,
        "worst": worst,
        "longest_win_streak": int(longest_win),
        "longest_loss_streak": int(abs(longest_loss)),
        "current_streak": int(cur),
        "by_exit_reason": by_reason,
    }


@router.get("/win-rate-trend")
async def win_rate_trend(window: int = 20, _user: str = Depends(require_auth)):
    """Rolling win rate / average R over the trailing *window* trades."""
    df = _trades_df()
    if df.empty or "pnl_net" not in df.columns:
        return {"window": window, "points": []}
    window = max(2, min(int(window), 200))

    ordered = df.copy()
    ordered["_exit"] = pd.to_datetime(ordered.get("exit_time"), errors="coerce")
    ordered = ordered.sort_values("_exit", kind="stable")
    pnl = pd.to_numeric(ordered["pnl_net"], errors="coerce")
    r = pd.to_numeric(ordered.get("r_multiple"), errors="coerce")

    points: List[Dict[str, Any]] = []
    wins = (pnl > 0).astype(float)
    roll_wr = wins.rolling(window, min_periods=1).mean()
    roll_r = r.rolling(window, min_periods=1).mean()
    for i in range(len(ordered)):
        row = ordered.iloc[i]
        points.append({
            "trade_id": row.get("trade_id"),
            "exit_time": str(row.get("exit_time", "")),
            "win_rate": round(float(roll_wr.iloc[i]), 4),
            "avg_r": (
                round(float(roll_r.iloc[i]), 3)
                if pd.notna(roll_r.iloc[i]) else None
            ),
        })
    return {"window": window, "points": points}


_DOW_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@router.get("/attribution")
async def attribution(_user: str = Depends(require_auth)):
    """Per-strategy P&L attribution + trade-timing heatmap (audit F-6).

    Computed from the completed-trade journal:

    * ``per_strategy`` — realized P&L, trade count, wins, and win-rate for each
      strategy, so it's clear which strategy (momentum / swing / short /
      selective) is making or losing money.
    * ``timing`` — realized P&L and trade count bucketed by exit day-of-week and
      hour-of-day, for a calendar/time-of-day heatmap.
    """
    df = _trades_df()
    empty = {"per_strategy": [], "timing": {"by_dow": [], "by_hour": []},
             "total_pnl": 0.0, "trades": 0}
    if df.empty or "pnl_net" not in df.columns:
        return empty

    work = df.copy()
    work["_pnl"] = pd.to_numeric(work["pnl_net"], errors="coerce").fillna(0.0)
    work["_strategy"] = work.get("strategy", "unknown").fillna("unknown").astype(str)
    work["_exit"] = pd.to_datetime(work.get("exit_time"), errors="coerce")

    # ── Per-strategy attribution ──
    per_strategy: List[Dict[str, Any]] = []
    for name, grp in work.groupby("_strategy"):
        pnl = grp["_pnl"]
        n = int(len(grp))
        wins = int((pnl > 0).sum())
        per_strategy.append({
            "strategy": name,
            "pnl": round(float(pnl.sum()), 2),
            "trades": n,
            "wins": wins,
            "win_rate": round(100.0 * wins / n, 1) if n else 0.0,
            "avg_pnl": round(float(pnl.mean()), 2) if n else 0.0,
        })
    per_strategy.sort(key=lambda r: r["pnl"], reverse=True)

    # ── Trade-timing heatmap (by exit time) ──
    timed = work[work["_exit"].notna()]
    by_dow: List[Dict[str, Any]] = []
    by_hour: List[Dict[str, Any]] = []
    if not timed.empty:
        dow = timed["_exit"].dt.dayofweek
        hour = timed["_exit"].dt.hour
        for d in range(7):
            mask = dow == d
            if not mask.any():
                continue
            sub = timed.loc[mask, "_pnl"]
            by_dow.append({"dow": d, "label": _DOW_LABELS[d],
                           "pnl": round(float(sub.sum()), 2),
                           "trades": int(len(sub))})
        for h in range(24):
            mask = hour == h
            if not mask.any():
                continue
            sub = timed.loc[mask, "_pnl"]
            by_hour.append({"hour": h, "pnl": round(float(sub.sum()), 2),
                            "trades": int(len(sub))})

    return {
        "per_strategy": per_strategy,
        "timing": {"by_dow": by_dow, "by_hour": by_hour},
        "total_pnl": round(float(work["_pnl"].sum()), 2),
        "trades": int(len(work)),
    }
