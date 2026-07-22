"""
Analytics & scanner API routes (features 5, 8, 12, 13, 14).

Bundles the read-mostly "insight" endpoints that back dashboard sections:
Monte Carlo projection, market-regime detection, strategy auto-tune preview,
the earnings calendar, and the pre-market scanner.  The two network-bound scans
(earnings, pre-market) run in a threadpool so they never block the event loop.
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

router = APIRouter(prefix="/api", tags=["Analytics"])


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


@router.get("/excursion")
async def excursion(_user: str = Depends(require_auth)):
    """MAE/MFE excursion distributions + stop/target-efficiency advisories.

    Reads the closed-trade journal (``trades.csv``), so it reflects every trade
    that carries excursion data; older pre-excursion trades are excluded from the
    distributions automatically.
    """
    from pathlib import Path

    from analytics.excursion import excursion_report, records_from_dataframe
    from analytics.performance import load_completed_trades

    settings = get_settings()

    def _build() -> dict:
        df = load_completed_trades(Path(settings.DATA_DIR) / "trades.csv")
        return excursion_report(records_from_dataframe(df))

    return await run_in_threadpool(_build)


@router.get("/earnings")
async def earnings(_user: str = Depends(require_auth)):
    """Upcoming earnings for every watchlist symbol (feature 5)."""
    from data.earnings_calendar import upcoming_earnings

    settings = get_settings()
    symbols = get_watchlist_store(settings.DATA_DIR).all_symbols()
    try:
        entries = await run_in_threadpool(upcoming_earnings, symbols)
    except Exception as exc:  # noqa: BLE001 -- fail-open, but make it visible
        log.warning("insights.earnings_failed", error=str(exc),
                    error_type=type(exc).__name__)
        entries = []
    return {
        "as_of": datetime.now(tz=EASTERN).isoformat(timespec="seconds"),
        "earnings": [e.to_dict() for e in entries],
    }


@router.get("/sectors")
async def sectors(_user: str = Depends(require_auth)):
    """Sector-rotation ranking + market-breadth participation (feature 3)."""
    from analytics.breadth import sector_breadth
    from signals.sector_rotation import rank_sectors

    settings = get_settings()

    def _compute():
        ranks = rank_sectors(settings)
        breadth = sector_breadth(settings)
        return ranks, breadth

    try:
        ranks, breadth = await run_in_threadpool(_compute)
    except Exception as exc:  # noqa: BLE001 -- fail-open, but make it visible
        log.warning("insights.sectors_failed", error=str(exc),
                    error_type=type(exc).__name__)
        ranks, breadth = [], None
    top_n = int(getattr(settings, "SECTOR_ROTATION_TOP_N", 3))
    leaders = [r.etf for r in ranks if r.rel_strength > 0 and r.above_ma50][:top_n]
    return {
        "as_of": datetime.now(tz=EASTERN).isoformat(timespec="seconds"),
        "enabled": bool(getattr(settings, "SECTOR_ROTATION_ENABLED", False)),
        "leaders": leaders,
        "ranking": [r.to_dict() for r in ranks],
        "breadth": breadth.to_dict() if breadth is not None else None,
    }


@router.get("/premarket")
async def premarket(_user: str = Depends(require_auth)):
    """Pre-market gap / unusual-volume scan of the watchlist (feature 14)."""
    from signals.premarket import scan

    settings = get_settings()
    symbols = get_watchlist_store(settings.DATA_DIR).scan_symbols()
    try:
        hits = await run_in_threadpool(scan, symbols, settings)
    except Exception as exc:  # noqa: BLE001 -- fail-open, but make it visible
        log.warning("insights.premarket_failed", error=str(exc),
                    error_type=type(exc).__name__)
        hits = []
    return {
        "as_of": datetime.now(tz=EASTERN).isoformat(timespec="seconds"),
        "thresholds": {
            "gap_pct": settings.PREMARKET_GAP_PCT,
            "volume_ratio": settings.PREMARKET_VOLUME_RATIO,
        },
        "hits": [h.to_dict() for h in hits],
    }
