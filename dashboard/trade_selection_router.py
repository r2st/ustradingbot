"""
Engine trade-selection API (feature 1).

Lets the operator pin the engine to chosen symbols / strategies / a minimum
setup grade from the dashboard, typically after reviewing backtest results.
Reading the selection needs only dashboard auth; changing what the engine is
allowed to trade requires the admin password (the same one that gates engine
control and manual trades).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from config.settings import get_settings
from config.trade_selection import (
    VALID_MIN_GRADES,
    VALID_STRATEGIES,
    TradeSelectionError,
    load_trade_selection,
    save_trade_selection,
)
from dashboard.auth import require_auth, verify_admin_password
from dashboard.rate_limit import rate_limit
from dashboard.schemas import TradeSelectionRequest

router = APIRouter(prefix="/api/trade-selection", tags=["Configuration"])


@router.get("")
async def get_selection(_user: str = Depends(require_auth)):
    """Current selection plus the option lists the form needs."""
    settings = get_settings()
    selection = load_trade_selection(settings.DATA_DIR)
    return {
        "selection": selection.to_dict(),
        "strategies": list(VALID_STRATEGIES),
        "grades": list(VALID_MIN_GRADES),
    }


@router.post("", dependencies=[Depends(rate_limit("trade_selection", control=True))])
async def set_selection(
    payload: TradeSelectionRequest, _user: str = Depends(require_auth)
):
    """Replace the selection (admin password required).

    Body: ``{enabled, symbols, strategies, min_grade, admin_password}``.  The
    domain validation (valid strategies / grades) stays in
    ``save_trade_selection``.  Rate-limited per client IP.
    """
    if not verify_admin_password(payload.admin_password):
        raise HTTPException(status_code=403, detail="Admin password required.")

    body = payload.model_dump(exclude_none=True)
    body.pop("admin_password", None)

    settings = get_settings()
    try:
        selection = save_trade_selection(settings.DATA_DIR, body)
    except TradeSelectionError as exc:
        return {"ok": False, "message": str(exc)}

    scope = []
    scope.append(f"{len(selection.symbols) or 'all'} symbols")
    scope.append(f"{len(selection.strategies) or 'all'} strategies")
    scope.append(f"grade ≥ {selection.min_grade}")
    state = "ENABLED" if selection.enabled else "disabled"
    return {
        "ok": True,
        "message": f"Trade selection {state} ({', '.join(scope)}). "
                   "The engine applies it on its next scan cycle.",
        "selection": selection.to_dict(),
    }
