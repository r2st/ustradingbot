"""
Daily earnings tracker API (Feature 2).

Dedicated ``APIRouter`` for the "reporting today" dashboard view: who reports
today (with beat/miss + EPS surprise once results land and pre/post-market move
when extended hours are enabled), same-sector contagion flags, and per-symbol
history.  Network-bound work runs in a threadpool so the event loop never
blocks; every handler degrades to empty on error (fail-open).

Mounted alongside the existing ``insights_router`` (which owns the *upcoming*
``/api/earnings`` list); the "today" endpoints live under ``/api/earnings/*``.
"""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from config.settings import EASTERN, get_settings
from config.watchlist import get_watchlist_store
from dashboard.auth import require_auth

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api", tags=["Market Data"])


def _record_history(settings, reporters) -> None:
    """Persist reported results to the append-only history store (best-effort)."""
    if not getattr(settings, "EARNINGS_HISTORY_ENABLED", True):
        return
    from data.earnings_tracker import record_result

    for de in reporters:
        if de.verdict is not None:  # only persist symbols whose result is known
            record_result(settings.DATA_DIR, de)


@router.get("/earnings/today")
async def earnings_today(_user: str = Depends(require_auth)):
    """Watchlist symbols reporting today, enriched with result + reaction."""
    from data.earnings_tracker import todays_earnings

    settings = get_settings()
    symbols = get_watchlist_store(settings.DATA_DIR).all_symbols()
    try:
        reporters = await run_in_threadpool(todays_earnings, symbols, settings)
    except Exception as exc:  # noqa: BLE001 -- fail-open, but make it visible
        log.warning("earnings.today_failed", error=str(exc),
                    error_type=type(exc).__name__)
        reporters = []
    if reporters:
        try:
            await run_in_threadpool(_record_history, settings, reporters)
        except Exception as exc:  # noqa: BLE001 -- history is best-effort
            log.warning("earnings.record_history_failed", error=str(exc),
                        error_type=type(exc).__name__)
    return {
        "as_of": datetime.now(tz=EASTERN).isoformat(timespec="seconds"),
        "reporters": [d.to_dict() for d in reporters],
    }


@router.get("/earnings/contagion")
async def earnings_contagion(_user: str = Depends(require_auth)):
    """Same-sector peers to watch when a bellwether reports a big surprise."""
    from data.earnings_tracker import sector_contagion, todays_earnings

    settings = get_settings()
    symbols = get_watchlist_store(settings.DATA_DIR).all_symbols()

    def _compute():
        reporters = todays_earnings(symbols, settings)
        alerts = []
        for de in reporters:
            peers = sector_contagion(de, symbols, settings)
            if peers:
                alerts.append({
                    "symbol": de.symbol,
                    "sector": de.sector,
                    "verdict": de.verdict,
                    "surprise_pct": de.surprise_pct,
                    "peers": peers,
                })
        return alerts

    try:
        alerts = await run_in_threadpool(_compute)
    except Exception as exc:  # noqa: BLE001 -- fail-open, but make it visible
        log.warning("earnings.contagion_failed", error=str(exc),
                    error_type=type(exc).__name__)
        alerts = []
    return {
        "as_of": datetime.now(tz=EASTERN).isoformat(timespec="seconds"),
        "threshold_pct": getattr(settings, "CONTAGION_SURPRISE_THRESHOLD", 5.0),
        "alerts": alerts,
    }


@router.get("/earnings/history/{symbol}")
async def earnings_history(symbol: str, _user: str = Depends(require_auth)):
    """Recorded past earnings results for *symbol*, newest first."""
    from data.earnings_tracker import symbol_history

    settings = get_settings()
    try:
        rows = await run_in_threadpool(symbol_history, settings.DATA_DIR, symbol)
    except Exception as exc:  # noqa: BLE001 -- fail-open, but make it visible
        log.warning("earnings.history_failed", symbol=str(symbol),
                    error=str(exc), error_type=type(exc).__name__)
        rows = []
    return {
        "symbol": str(symbol).upper(),
        "history": rows,
    }
