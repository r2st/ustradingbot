"""
Manual trade entry API (feature 2).

Admin-gated: placing a trade by hand requires the admin password (the same one
that gates going live / engine control), supplied in the JSON body as
``admin_password``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from config.settings import get_settings
from dashboard.auth import require_auth, verify_admin_password
from dashboard.rate_limit import rate_limit
from dashboard.schemas import ManualTradeRequest

router = APIRouter(prefix="/api/manual-trade", tags=["Trading"])


@router.post("", dependencies=[Depends(rate_limit("manual_trade"))])
async def place(payload: ManualTradeRequest, _user: str = Depends(require_auth)):
    """Validate + place a manual bracket order (admin password required).

    Scalar type/bounds validation is enforced by :class:`ManualTradeRequest`
    (bad input → 422) before the admin-password gate; the deeper cross-field
    rules (stop-below-entry, ladder consistency, symbol normalisation) stay in
    ``place_manual_trade``.  Rate-limited per client IP (money path).
    """
    if not verify_admin_password(payload.admin_password):
        raise HTTPException(status_code=403, detail="Admin password required.")

    from execution.manual_trade import place_manual_trade

    settings = get_settings()
    # Drop unset optionals so validate_params still falls back to its ladder /
    # legacy stop-target logic exactly as before; the admin password is not a
    # trade parameter.
    body = payload.model_dump(exclude_none=True)
    body.pop("admin_password", None)
    # place_manual_trade builds a broker and touches the filesystem — run it off
    # the event loop.
    result = await run_in_threadpool(place_manual_trade, body, settings)
    return result.to_dict()
