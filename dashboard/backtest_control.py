"""
Background backtest jobs for the dashboard.

A backtest blocks for seconds to minutes (it fetches history and replays the
strategies bar-by-bar), so the dashboard runs each one in a daemon thread and
exposes progress + results through an in-memory job registry.  The caller gets
a ``job_id`` immediately and polls :func:`get_job` until the state is ``done``
or ``error``.

The registry is bounded (oldest jobs evicted) to cap memory and is
process-local — the dashboard runs a single uvicorn worker, so every request
sees the same registry.
"""

from __future__ import annotations

import re
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

import pandas as pd
import structlog

from config.settings import get_settings
from config.universe import ALL_SYMBOLS

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: Retain at most this many jobs (oldest evicted first).
_MAX_JOBS = 20

#: Guard rail: cap symbols per run so a huge universe cannot wedge the worker.
_MAX_SYMBOLS = 40

#: Cap the trade rows returned to the UI so the JSON payload stays reasonable.
_MAX_TRADES_RETURNED = 1000

#: Cap the event-log rows returned to the UI (the log viewer paginates these).
_MAX_EVENTS_RETURNED = 3000

#: Strategies the backtester supports, as ``(value, label, default_on)`` for the
#: UI.  PEAD defaults off: it fetches live earnings data per symbol per bar, so
#: it is markedly slower than the price-only strategies.
STRATEGIES: List[Tuple[str, str, bool]] = [
    ("vcp_breakout", "VCP Breakout", True),
    ("momentum", "Momentum", True),
    ("swing", "Swing", True),
    ("mean_reversion", "Mean Reversion", True),
    ("pead", "PEAD (earnings drift, slower)", False),
]
_VALID_STRATEGIES = {value for value, _, _ in STRATEGIES}

_GRADES = ["A", "B", "C"]

_jobs: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Form options
# ---------------------------------------------------------------------------


def options() -> Dict[str, Any]:
    """Return the choices the backtest form needs (symbols, strategies, dates)."""
    end = datetime.now().date()
    start = end - timedelta(days=365)
    return {
        "symbols": list(ALL_SYMBOLS),
        "strategies": [
            {"value": v, "label": label, "default": default_on}
            for v, label, default_on in STRATEGIES
        ],
        "grades": list(_GRADES),
        "defaults": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "min_grade": "B",
            "starting_capital": float(get_settings().TOTAL_CAPITAL),
            "max_symbols": _MAX_SYMBOLS,
        },
    }


# ---------------------------------------------------------------------------
# Job lifecycle
# ---------------------------------------------------------------------------


def _build_config(params: Dict[str, Any]):
    """Validate raw form *params* into a ``BacktestConfig`` (+ echoed meta).

    Raises:
        ValueError: with a user-facing message when the inputs are invalid.
    """
    from backtest.engine import BacktestConfig

    raw_syms = params.get("symbols") or []
    if isinstance(raw_syms, str):
        raw_syms = [s for s in re.split(r"[,\s]+", raw_syms) if s]
    symbols: List[str] = []
    seen: set[str] = set()
    for sym in raw_syms:
        upper = str(sym).strip().upper()
        if upper and upper not in seen:
            seen.add(upper)
            symbols.append(upper)
    if not symbols:
        raise ValueError("Select at least one symbol.")
    if len(symbols) > _MAX_SYMBOLS:
        raise ValueError(
            f"Too many symbols ({len(symbols)}); the maximum is {_MAX_SYMBOLS}."
        )

    strategies = [
        str(s).strip().lower()
        for s in (params.get("strategies") or [])
    ]
    strategies = [s for s in strategies if s in _VALID_STRATEGIES]
    if not strategies:
        raise ValueError("Select at least one strategy.")

    start = str(params.get("start", "")).strip()
    end = str(params.get("end", "")).strip()
    if not start or not end:
        raise ValueError("Both a start and end date are required.")
    try:
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
    except (ValueError, TypeError):
        raise ValueError("Invalid date format — use YYYY-MM-DD.")
    if start_ts >= end_ts:
        raise ValueError("The start date must be before the end date.")

    min_grade = str(params.get("min_grade", "B")).strip().upper() or "B"
    if min_grade not in _GRADES:
        min_grade = "B"

    raw_capital = params.get("starting_capital")
    if raw_capital in (None, ""):
        capital = float(get_settings().TOTAL_CAPITAL)
    else:
        try:
            capital = float(raw_capital)
        except (ValueError, TypeError):
            raise ValueError("Starting capital must be a number.")
    if capital <= 0:
        raise ValueError("Starting capital must be a positive number.")

    config = BacktestConfig(
        symbols=symbols,
        start=start,
        end=end,
        strategies=strategies,
        min_grade=min_grade,
        starting_capital=capital,
    )
    meta = {
        "symbols": symbols,
        "strategies": strategies,
        "start": start,
        "end": end,
        "min_grade": min_grade,
        "starting_capital": capital,
    }
    return config, meta


def start_backtest(params: Dict[str, Any]) -> Dict[str, Any]:
    """Validate *params*, spawn a background run, and return ``{ok, job_id}``."""
    try:
        config, meta = _build_config(params)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}

    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "state": "running",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "params": meta,
        "message": "Loading price history and replaying strategies…",
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        while len(_jobs) > _MAX_JOBS:
            _jobs.popitem(last=False)

    thread = threading.Thread(
        target=_run_job, args=(job_id, config), daemon=True,
        name=f"backtest-{job_id}",
    )
    thread.start()
    log.info("backtest.ui_started", job_id=job_id, **meta)
    return {"ok": True, "job_id": job_id}


def _run_job(job_id: str, config) -> None:
    """Execute one backtest in the background and store its outcome."""
    from backtest.engine import run_backtest

    try:
        result = run_backtest(config)
        _update(
            job_id,
            state="done",
            message="Backtest complete.",
            result=_summarize(result),
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        log.info(
            "backtest.ui_complete",
            job_id=job_id,
            trades=result.summary.get("total_trades"),
            total_pnl=result.summary.get("total_pnl"),
        )
    except Exception as exc:  # noqa: BLE001 -- surface any failure to the UI
        log.exception("backtest.ui_failed", job_id=job_id)
        _update(
            job_id,
            state="error",
            message=f"Backtest failed: {exc}",
            error=str(exc),
            finished_at=datetime.now(timezone.utc).isoformat(),
        )


def _summarize(result) -> Dict[str, Any]:
    """Reduce a ``BacktestResult`` to the JSON payload the UI renders."""
    payload = result.to_dict()
    trades = payload.get("trades", [])
    payload["trade_count"] = len(trades)
    if len(trades) > _MAX_TRADES_RETURNED:
        # Keep the most recent trades; flag the truncation for the UI.
        payload["trades"] = trades[-_MAX_TRADES_RETURNED:]
        payload["truncated"] = True
    else:
        payload["truncated"] = False

    # Event log for the dashboard log viewer.  events_total counts everything
    # the engine produced; events may already be capped at the engine's limit,
    # and we cap again for the UI payload.
    events = payload.get("events", [])
    payload["events_total"] = payload.get("events_total", len(events))
    if len(events) > _MAX_EVENTS_RETURNED:
        payload["events"] = events[:_MAX_EVENTS_RETURNED]
        payload["events_truncated"] = True
    else:
        payload["events_truncated"] = False
    return payload


def _update(job_id: str, **fields: Any) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(fields)


def get_job(job_id: str) -> Dict[str, Any] | None:
    """Return a copy of the job record, or ``None`` when unknown/evicted."""
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job is not None else None
