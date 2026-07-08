"""
Per-position actions API — stop (close) a live trade from the dashboard.

Admin-gated like manual trades and engine control: closing a position moves
money, so the admin password (``DASHBOARD_PASSWORD``) is required in the JSON
body as ``admin_password``.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from config.settings import get_settings
from dashboard.auth import require_auth, verify_admin_password

router = APIRouter(prefix="/api/positions", tags=["positions"])


@router.post("/stop")
async def stop_position(request: Request, _user: str = Depends(require_auth)):
    """Close an open position at market (admin password required).

    Body: ``{symbol, admin_password}``.
    """
    try:
        body: Dict[str, Any] = await request.json()
    except (ValueError, TypeError):
        body = {}
    if not isinstance(body, dict):
        body = {}

    if not verify_admin_password(str(body.get("admin_password", ""))):
        raise HTTPException(status_code=403, detail="Admin password required.")

    symbol = str(body.get("symbol", "")).strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="A symbol is required.")

    from execution.stop_trade import stop_open_position

    settings = get_settings()
    # Builds a broker and touches the filesystem — run it off the event loop.
    result = await run_in_threadpool(stop_open_position, symbol, settings)
    return result.to_dict()
