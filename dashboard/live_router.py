"""
Live P&L + position-monitoring API (monitoring features 1 and 3).

``GET /api/live/pnl`` marks every open position to market through the shared
quote service and returns per-position unrealized P&L plus the F3 monitoring
fields (distance to stop/target, R-progress, time in trade, proximity flag),
along with portfolio totals (unrealized, realized today, all-time).

Each hit also samples the intraday P&L series (throttled to one point per
minute) into ``DATA_DIR/pnl_intraday.jsonl`` so the dashboard can chart
today's P&L; ``GET /api/live/pnl/intraday`` reads it back.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import structlog
from fastapi import APIRouter, Depends

from dashboard.auth import get_settings, require_auth

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/live", tags=["Analytics"])

ET = ZoneInfo("America/New_York")
INTRADAY_FILE = "pnl_intraday.jsonl"
_SAMPLE_MIN_INTERVAL = 60.0  # seconds between persisted intraday points

_sample_lock = threading.Lock()
_last_sample_monotonic = 0.0


def _load_positions(data_dir: Path) -> List[Dict[str, Any]]:
    path = data_dir / "open_positions.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return list(data.values()) if isinstance(data, dict) else []


def _realized(data_dir: Path) -> Dict[str, float]:
    """Return all-time and today's realized net P&L from the journal."""
    from analytics.performance import load_completed_trades

    df = load_completed_trades(data_dir / "trades.csv")
    if df.empty or "pnl_net" not in df.columns:
        return {"all_time": 0.0, "today": 0.0}
    pnl = pd.to_numeric(df["pnl_net"], errors="coerce").fillna(0.0)
    all_time = float(pnl.sum())
    today = 0.0
    if "exit_time" in df.columns:
        exits = pd.to_datetime(df["exit_time"], errors="coerce")
        mask = exits.dt.date == datetime.now(tz=ET).date()
        today = float(pnl[mask].sum())
    return {"all_time": round(all_time, 2), "today": round(today, 2)}


def _effective_levels(pos: Dict[str, Any]) -> Dict[str, Any]:
    """Return the effective stop/target, honouring manual exit ladders.

    For laddered manual trades the *nearest untriggered rung* of each kind is
    the effective level (mirrors ``_PaperPosition`` semantics); classic
    positions just use ``stop_price`` / ``target_price``.
    """
    stop = float(pos.get("stop_price", 0) or 0) or None
    target = float(pos.get("target_price", 0) or 0) or None
    next_level = None
    levels = pos.get("levels") or []
    if levels:
        side = str(pos.get("direction", "long") or "long").lower()
        stops = [l for l in levels
                 if l.get("kind") == "stop" and not l.get("triggered")]
        targets = [l for l in levels
                   if l.get("kind") == "target" and not l.get("triggered")]
        try:
            if stops:
                # Nearest stop: highest below price for longs, lowest for shorts.
                stop = (max if side == "long" else min)(
                    float(l.get("price", 0) or 0) for l in stops
                )
            if targets:
                target = (min if side == "long" else max)(
                    float(l.get("price", 0) or 0) for l in targets
                )
            untriggered = stops + targets
            if untriggered:
                nearest = untriggered[0]
                next_level = {
                    "kind": nearest.get("kind"),
                    "price": float(nearest.get("price", 0) or 0),
                    "quantity": int(nearest.get("quantity", 0) or 0),
                }
        except (ValueError, TypeError):
            pass
    return {"stop": stop, "target": target, "next_level": next_level}


def progress_to_target_pct(
    entry: Optional[float],
    current: Optional[float],
    target: Optional[float],
    side: str = "long",
) -> Optional[float]:
    """Percent of the entry→target journey the price has travelled (0-100).

    Powers the "68% to target" progress bar in the position *card* view. For a
    long, 0% sits at the entry price and 100% at the target; a short is mirrored
    (progress rises as price falls). The result is clamped to ``[0, 100]`` so a
    position trading beyond its target or below its entry still renders a sane
    bar. Returns ``None`` when the inputs are missing or degenerate (entry equal
    to target), matching the "—" fallback used elsewhere.
    """
    if entry is None or current is None or target is None:
        return None
    try:
        entry = float(entry)
        current = float(current)
        target = float(target)
    except (TypeError, ValueError):
        return None
    span = (target - entry) if str(side).lower() != "short" else (entry - target)
    if span == 0:
        return None
    travelled = (current - entry) if str(side).lower() != "short" else (entry - current)
    pct = travelled / span * 100.0
    return round(max(0.0, min(100.0, pct)), 1)


def _position_row(
    pos: Dict[str, Any],
    quote: Dict[str, Any],
    settings,
) -> Dict[str, Any]:
    symbol = str(pos.get("symbol", ""))
    side = str(pos.get("direction", "long") or "long").lower()
    try:
        entry = float(pos.get("entry_price", 0) or 0)
        qty = int(float(pos.get("quantity", 0) or 0))
    except (ValueError, TypeError):
        entry, qty = 0.0, 0
    levels = _effective_levels(pos)
    stop, target = levels["stop"], levels["target"]
    current = quote.get("price") if quote else None
    sign = -1.0 if side == "short" else 1.0

    row: Dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "strategy": pos.get("strategy", ""),
        "grade": pos.get("grade", ""),
        "quantity": qty,
        "entry_price": round(entry, 4),
        "stop_price": round(stop, 4) if stop else None,
        "target_price": round(target, 4) if target else None,
        "currency": str(pos.get("currency", "USD")).upper(),
        "current_price": round(float(current), 4) if current is not None else None,
        "stale": current is None,
        "unrealized_pnl": None,
        "unrealized_pct": None,
        "distance_to_stop_pct": None,
        "distance_to_target_pct": None,
        "progress_to_target_pct": None,
        "r_progress": None,
        "proximity": None,
        "partial_taken": bool(pos.get("partial_taken", False)),
        "manual": bool(pos.get("manual", False)),
        "next_level": levels["next_level"],
    }

    # Time in trade vs the max-hold budget.
    entry_time = pos.get("entry_time") or pos.get("opened_at")
    try:
        opened = datetime.fromisoformat(str(entry_time))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=ET)
        else:
            opened = opened.astimezone(ET)
        hours = (datetime.now(tz=ET) - opened).total_seconds() / 3600.0
        row["time_in_trade_hours"] = round(max(0.0, hours), 1)
    except (ValueError, TypeError):
        row["time_in_trade_hours"] = None
    row["max_hold_hours"] = float(settings.HOLD_MAX_DAYS) * 24.0
    row["entry_time"] = str(entry_time or "")[:19].replace("T", " ")

    if current is None:
        return row

    current = float(current)
    move = (current - entry) * sign
    row["unrealized_pnl"] = round(move * qty, 2)
    row["unrealized_pct"] = round(move / entry * 100.0, 2) if entry > 0 else None

    if current > 0 and stop:
        row["distance_to_stop_pct"] = round(
            abs(current - stop) / current * 100.0, 2
        )
    if current > 0 and target:
        row["distance_to_target_pct"] = round(
            abs(target - current) / current * 100.0, 2
        )
    row["progress_to_target_pct"] = progress_to_target_pct(
        entry, current, target, side
    )
    if stop:
        risk = (entry - stop) * sign
        if risk > 0:
            row["r_progress"] = round(move / risk, 2)

    prox_pct = float(settings.POSITION_PROXIMITY_ALERT_PCT)
    if row["distance_to_stop_pct"] is not None \
            and row["distance_to_stop_pct"] <= prox_pct:
        row["proximity"] = "near_stop"
    elif row["distance_to_target_pct"] is not None \
            and row["distance_to_target_pct"] <= prox_pct:
        row["proximity"] = "near_target"
    return row


def _sample_intraday(data_dir: Path, totals: Dict[str, Any]) -> None:
    """Append an intraday P&L point (throttled; best-effort)."""
    global _last_sample_monotonic
    with _sample_lock:
        now = time.monotonic()
        if now - _last_sample_monotonic < _SAMPLE_MIN_INTERVAL:
            return
        _last_sample_monotonic = now
    try:
        now_et = datetime.now(tz=ET)
        point = {
            "ts": now_et.isoformat(),
            "date": now_et.date().isoformat(),
            "unrealized": totals.get("unrealized", 0.0),
            "realized_today": totals.get("realized_today", 0.0),
            "total": totals.get("today_total", 0.0),
        }
        path = data_dir / INTRADAY_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        # Keep the file bounded: rotate at ~2 MB (months of 1/min sampling).
        try:
            if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
                import os

                os.replace(path, path.with_suffix(path.suffix + ".1"))
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(point) + "\n")
    except Exception:  # noqa: BLE001 -- sampling must never fail the endpoint
        # Persisting the intraday sample is best-effort, but log the failure so
        # a broken/unwritable data dir is diagnosable (B-9).
        log.debug("live.intraday_sample_failed", exc_info=True)


def build_pnl_snapshot(settings) -> Dict[str, Any]:
    """Mark every open position to market and return the F1+F3 P&L snapshot.

    Shared by ``GET /api/live/pnl`` and the ``/ws/pnl`` WebSocket so both emit
    byte-identical payloads.  Synchronous (does the quote fetch inline); call
    it from a threadpool inside async contexts to avoid blocking the loop.
    """
    from dashboard import quotes

    data_dir = Path(settings.DATA_DIR)
    positions_raw = _load_positions(data_dir)
    symbols = [str(p.get("symbol", "")) for p in positions_raw if p.get("symbol")]
    quote_map = quotes.get_quotes(symbols) if symbols else {}

    rows = [
        _position_row(p, quote_map.get(str(p.get("symbol", "")), {}), settings)
        for p in positions_raw
    ]
    # Most urgent first: nearest-to-stop on top, stale rows last.
    rows.sort(key=lambda r: (
        r["distance_to_stop_pct"] is None,
        r["distance_to_stop_pct"] if r["distance_to_stop_pct"] is not None else 1e9,
    ))

    realized = _realized(data_dir)
    unrealized = round(
        sum(r["unrealized_pnl"] for r in rows if r["unrealized_pnl"] is not None), 2
    )
    priced = [r for r in rows if not r["stale"]]
    totals = {
        "unrealized": unrealized,
        "realized_today": realized["today"],
        "today_total": round(unrealized + realized["today"], 2),
        "realized_all_time": realized["all_time"],
        "account_equity": round(
            float(settings.TOTAL_CAPITAL) + realized["all_time"] + unrealized, 2
        ),
        "positions_priced": len(priced),
        "positions_total": len(rows),
    }

    now_et = datetime.now(tz=ET)
    # Oldest quote age across priced rows (for the staleness badge).
    quote_age = None
    ages = []
    for r in priced:
        q = quote_map.get(r["symbol"]) or {}
        try:
            fetched = datetime.fromisoformat(str(q.get("fetched_at", "")))
            ages.append((datetime.now(fetched.tzinfo) - fetched).total_seconds())
        except (ValueError, TypeError):
            continue
    if ages:
        quote_age = round(max(ages), 1)

    _sample_intraday(data_dir, totals)

    return {
        "as_of": now_et.isoformat(),
        "quote_age_seconds": quote_age,
        "positions": rows,
        "totals": totals,
    }


@router.get("/pnl")
async def live_pnl(_user: str = Depends(require_auth)):
    """Live unrealized P&L per open position + portfolio totals (F1 + F3)."""
    return build_pnl_snapshot(get_settings())


@router.get("/pnl/intraday")
async def live_pnl_intraday(date: str = "", _user: str = Depends(require_auth)):
    """Sampled intraday P&L points for *date* (defaults to today, ET)."""
    settings = get_settings()
    want = date or datetime.now(tz=ET).date().isoformat()
    path = Path(settings.DATA_DIR) / INTRADAY_FILE
    points: List[Dict[str, Any]] = []
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict) and rec.get("date") == want:
                        points.append(rec)
        except OSError:
            pass
    return {"date": want, "points": points}
