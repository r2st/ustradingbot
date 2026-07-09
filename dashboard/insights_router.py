"""
Analytics & scanner API routes (features 5, 8, 12, 13, 14).

Bundles the read-mostly "insight" endpoints that back dashboard sections:
Monte Carlo projection, market-regime detection, strategy auto-tune preview,
the earnings calendar, and the pre-market scanner.  The two network-bound scans
(earnings, pre-market) run in a threadpool so they never block the event loop.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from config.settings import EASTERN, get_settings
from config.watchlist import get_watchlist_store
from dashboard.auth import require_auth

router = APIRouter(prefix="/api", tags=["insights"])


@router.get("/montecarlo")
async def montecarlo(runs: int = 0, horizon: int = 0, _user: str = Depends(require_auth)):
    """Monte Carlo equity projection from the trade journal (feature 8)."""
    from analytics.montecarlo import run_from_journal

    settings = get_settings()
    result = await run_in_threadpool(
        run_from_journal, settings, runs or None, horizon or None
    )
    return result.to_dict()


@router.get("/regime")
async def regime(_user: str = Depends(require_auth)):
    """Current market-regime detection + strategy weight multipliers (feature 13)."""
    from analytics.regime import current_regime

    settings = get_settings()
    result = await run_in_threadpool(current_regime, settings)
    return result.to_dict()


@router.get("/autotune")
async def autotune(_user: str = Depends(require_auth)):
    """Preview the adaptive grade thresholds from recent performance (feature 12)."""
    from automation.autotune import tune_from_journal

    settings = get_settings()
    result = await run_in_threadpool(tune_from_journal, settings)
    return result.to_dict()


@router.get("/earnings")
async def earnings(_user: str = Depends(require_auth)):
    """Upcoming earnings for every watchlist symbol (feature 5)."""
    from data.earnings_calendar import upcoming_earnings

    settings = get_settings()
    symbols = get_watchlist_store(settings.DATA_DIR).all_symbols()
    try:
        entries = await run_in_threadpool(upcoming_earnings, symbols)
    except Exception:  # noqa: BLE001
        entries = []
    return {
        "as_of": datetime.now(tz=EASTERN).isoformat(timespec="seconds"),
        "earnings": [e.to_dict() for e in entries],
    }


@router.get("/premarket")
async def premarket(_user: str = Depends(require_auth)):
    """Pre-market gap / unusual-volume scan of the watchlist (feature 14)."""
    from signals.premarket import scan

    settings = get_settings()
    symbols = get_watchlist_store(settings.DATA_DIR).scan_symbols()
    try:
        hits = await run_in_threadpool(scan, symbols, settings)
    except Exception:  # noqa: BLE001
        hits = []
    return {
        "as_of": datetime.now(tz=EASTERN).isoformat(timespec="seconds"),
        "thresholds": {
            "gap_pct": settings.PREMARKET_GAP_PCT,
            "volume_ratio": settings.PREMARKET_VOLUME_RATIO,
        },
        "hits": [h.to_dict() for h in hits],
    }
