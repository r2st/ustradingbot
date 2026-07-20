"""
Analyst dashboard API + page (feature TA2).

Serves the AI commentary payload built by :mod:`dashboard.ai_commentary` and
the full-page Analyst view at ``/ai-dashboard``.  Commentary generation never
blocks a request: ``GET /api/ai/commentary`` returns the cached payload
immediately and, when it is stale and the market is open, schedules one
background refresh on the running event loop (stale-while-revalidate).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, Set

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from dashboard.auth import require_auth
from dashboard.rate_limit import rate_limit

log = structlog.get_logger(__name__)

router = APIRouter(tags=["AI"])

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

# Keep strong references to in-flight refresh tasks so the event loop
# doesn't garbage-collect them mid-run.
_refresh_tasks: Set[asyncio.Task] = set()


def _engine():
    from dashboard.ai_commentary import get_engine

    return get_engine()


def _schedule_refresh(engine, force: bool = False) -> None:
    """Run one refresh in the background (fire-and-forget, deduplicated)."""
    task = asyncio.get_running_loop().create_task(engine.refresh(force=force))
    _refresh_tasks.add(task)
    task.add_done_callback(_refresh_tasks.discard)


@router.get("/ai-dashboard", response_class=HTMLResponse)
async def ai_dashboard_page(request: Request, _user: str = Depends(require_auth)):
    """Render the full-page Analyst dashboard."""
    return _templates.TemplateResponse(
        request,
        "ai_dashboard.html",
        headers={"Cache-Control": "no-store"},
        context={},
    )


@router.get(
    "/api/ai/commentary",
    dependencies=[Depends(rate_limit("ai_commentary", ai=True))],
)
async def ai_commentary(_user: str = Depends(require_auth)) -> Dict[str, Any]:
    """All three panels' commentary (stale-while-revalidate).

    Serves whatever is cached instantly; a refresh is scheduled in the
    background when the payload is older than the configured interval, the
    market is open (or nothing has ever been generated), and a client has
    polled recently.
    """
    engine = _engine()
    engine.note_poll()
    if engine.should_refresh():
        _schedule_refresh(engine)
    return engine.payload_for_client()


@router.get(
    "/api/ai/market-overview",
    dependencies=[Depends(rate_limit("ai_market_overview", ai=True))],
)
async def ai_market_overview(_user: str = Depends(require_auth)) -> Dict[str, Any]:
    """Market conditions only (regime, VIX bucket, sectors, bias, summary)."""
    engine = _engine()
    engine.note_poll()
    if engine.should_refresh():
        _schedule_refresh(engine)
    payload = engine.payload_for_client()
    return {
        "generated_at": payload.get("generated_at"),
        "stale": payload.get("stale"),
        "market_open": payload.get("market_open"),
        "market": payload.get("market") or {},
    }


@router.post(
    "/api/ai/refresh",
    dependencies=[Depends(rate_limit("ai_refresh", ai=True))],
)
async def ai_refresh(_user: str = Depends(require_auth)) -> Dict[str, Any]:
    """Force a commentary refresh outside the timer (still budget-checked).

    Returns 429 when the daily LLM budget is exhausted — the scheduled
    template-only refreshes keep running regardless.
    """
    engine = _engine()
    engine.note_poll()
    if engine.budget_exhausted():
        raise HTTPException(
            status_code=429,
            detail="Daily AI commentary budget exhausted; "
                   "template commentary remains available.",
        )
    _schedule_refresh(engine, force=True)
    return {"ok": True, "started": True, "status": engine.status()}


@router.get("/api/ai/status")
async def ai_status(_user: str = Depends(require_auth)) -> Dict[str, Any]:
    """Commentary engine health: last run, last error, budget, model."""
    return _engine().status()
