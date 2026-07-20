"""
Engine activity feed API (monitoring feature 4).

Reads the structured event stream the engine appends to
``DATA_DIR/engine_activity.jsonl`` (see :mod:`journal.activity_log`) and
serves it newest-first with filters plus a per-cycle rollup.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from dashboard.auth import get_settings, require_auth
from journal.activity_log import cycles_summary, read_activity

router = APIRouter(prefix="/api/activity", tags=["Journal"])


@router.get("")
async def activity_feed(
    limit: int = 200,
    since_ts: str = "",
    event: str = "",
    symbol: str = "",
    cycle_id: str = "",
    _user: str = Depends(require_auth),
):
    """Newest-first engine activity events, filterable; ``since_ts`` enables
    incremental polling (only events newer than the given ISO timestamp)."""
    settings = get_settings()
    events = read_activity(
        settings.DATA_DIR,
        limit=max(1, min(int(limit), 1000)),
        since_ts=since_ts or None,
        event=event or None,
        symbol=symbol or None,
        cycle_id=cycle_id or None,
    )
    return {"events": events, "count": len(events)}


@router.get("/cycles")
async def activity_cycles(limit: int = 20, _user: str = Depends(require_auth)):
    """One rolled-up row per engine cycle, newest first."""
    settings = get_settings()
    cycles = cycles_summary(settings.DATA_DIR, limit=max(1, min(int(limit), 200)))
    return {"cycles": cycles}
