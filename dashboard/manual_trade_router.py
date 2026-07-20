"""
Manual trade entry API (feature 2).

Admin-gated: placing a trade by hand requires the admin password (the same one
that gates going live / engine control), supplied in the JSON body as
``admin_password``.

Every attempt on this money path is written to a structured audit trail
(``manual_trade.*`` events) recording *who* placed *what*, for which
symbol/quantity/side, from which IP — so a live order can always be
reconstructed post-incident (audit B-2).  A rejected admin password is logged at
WARN, mirroring ``engine_control.bad_password``.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from config.settings import get_settings
from dashboard.auth import require_auth, verify_admin_password
from dashboard.rate_limit import rate_limit
from dashboard.schemas import ManualTradeRequest

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/manual-trade", tags=["Trading"])


def _client_ip(request: Request | None) -> str:
    """Best-effort client IP for the audit line, honouring ``X-Forwarded-For``."""
    if request is None:
        return "unknown"
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"


@router.post("", dependencies=[Depends(rate_limit("manual_trade"))])
async def place(
    payload: ManualTradeRequest,
    request: Request,
    _user: str = Depends(require_auth),
):
    """Validate + place a manual bracket order (admin password required).

    Scalar type/bounds validation is enforced by :class:`ManualTradeRequest`
    (bad input → 422) before the admin-password gate; the deeper cross-field
    rules (stop-below-entry, ladder consistency, symbol normalisation) stay in
    ``place_manual_trade``.  Rate-limited per client IP (money path).

    Emits an audit record on every outcome: a WARN on a rejected admin password
    and an INFO carrying the full trade envelope on placement (audit B-2).
    """
    ip = _client_ip(request)
    symbol = (payload.symbol or "").upper()
    side = getattr(payload, "side", None) or "buy"

    if not verify_admin_password(payload.admin_password):
        log.warning(
            "manual_trade.bad_password",
            symbol=symbol,
            quantity=payload.quantity,
            side=side,
            user=_user,
            ip=ip,
        )
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

    # Audit trail for the single most sensitive action in the system: log the
    # full trade envelope (who/what/when/where) on both success and failure.
    event = "manual_trade.placed" if result.ok else "manual_trade.rejected"
    emit = log.info if result.ok else log.warning
    emit(
        event,
        symbol=result.symbol or symbol,
        quantity=result.quantity or payload.quantity,
        side=side,
        entry_price=payload.entry_price,
        stop_price=payload.stop_price,
        target_price=payload.target_price,
        fill_price=result.fill_price,
        order_id=result.order_id,
        trading_mode=settings.TRADING_MODE,
        broker=settings.BROKER,
        user=_user,
        ip=ip,
        ok=result.ok,
        message=result.message,
    )
    return result.to_dict()
