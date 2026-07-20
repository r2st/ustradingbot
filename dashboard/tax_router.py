"""
Tax / realized-gains reporting API (feature P1f).

Serves the FIFO cost-basis report, short-term / long-term gains split, and
wash-sale flags under ``/api/tax``.  All computation runs in a threadpool since
it parses the trade journal.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from config.settings import get_settings
from dashboard.auth import require_auth

router = APIRouter(prefix="/api/tax", tags=["Analytics"])


@router.get("/report")
async def tax_report(year: Optional[int] = None, _user: str = Depends(require_auth)):
    """Realized-gains report; pass ``?year=YYYY`` to scope to one tax year."""
    from analytics.tax import build_tax_report

    settings = get_settings()
    report = await run_in_threadpool(build_tax_report, settings.DATA_DIR, year)
    return report.to_dict()


@router.get("/years")
async def tax_years(_user: str = Depends(require_auth)):
    """The distinct tax years present in the journal (newest first for the UI)."""
    from analytics.tax import build_tax_report

    settings = get_settings()
    report = await run_in_threadpool(build_tax_report, settings.DATA_DIR, None)
    return {"years": sorted(report.available_years, reverse=True)}
