"""
Per-position actions API — stop (close) a live trade from the dashboard.

Admin-gated like manual trades and engine control: closing a position moves
money, so the admin password (``DASHBOARD_PASSWORD``) is required in the JSON
body as ``admin_password``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from config.settings import get_settings
from dashboard.auth import require_auth, verify_admin_password
from dashboard.rate_limit import rate_limit
from dashboard.schemas import PositionStopRequest

router = APIRouter(prefix="/api/positions", tags=["Trading"])


@router.post("/stop", dependencies=[Depends(rate_limit("position_stop"))])
async def stop_position(
    payload: PositionStopRequest, _user: str = Depends(require_auth)
):
    """Close an open position at market (admin password required).

    Body: ``{symbol, admin_password}``.  Symbol/type validation → 422; the admin
    password gate follows.  Rate-limited per client IP (money path).
    """
    if not verify_admin_password(payload.admin_password):
        raise HTTPException(status_code=403, detail="Admin password required.")

    symbol = payload.symbol.strip().upper()

    from execution.stop_trade import stop_open_position

    settings = get_settings()
    # Builds a broker and touches the filesystem — run it off the event loop.
    result = await run_in_threadpool(stop_open_position, symbol, settings)
    return result.to_dict()
