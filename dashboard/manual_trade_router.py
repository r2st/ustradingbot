"""
Manual trade entry API (feature 2).

Admin-gated: placing a trade by hand requires the admin password (the same one
that gates going live / engine control), supplied in the JSON body as
``admin_password``.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from config.settings import get_settings
from dashboard.auth import require_auth, verify_admin_password

router = APIRouter(prefix="/api/manual-trade", tags=["manual-trade"])


@router.post("")
async def place(request: Request, _user: str = Depends(require_auth)):
    """Validate + place a manual bracket order (admin password required)."""
    try:
        body: Dict[str, Any] = await request.json()
    except (ValueError, TypeError):
        body = {}
    if not isinstance(body, dict):
        body = {}

    if not verify_admin_password(str(body.get("admin_password", ""))):
        raise HTTPException(status_code=403, detail="Admin password required.")

    from execution.manual_trade import place_manual_trade

    settings = get_settings()
    # place_manual_trade builds a broker and touches the filesystem — run it off
    # the event loop.
    result = await run_in_threadpool(place_manual_trade, body, settings)
    return result.to_dict()
