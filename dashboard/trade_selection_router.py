"""
Engine trade-selection API (feature 1).

Lets the operator pin the engine to chosen symbols / strategies / a minimum
setup grade from the dashboard, typically after reviewing backtest results.
Reading the selection needs only dashboard auth; changing what the engine is
allowed to trade requires the admin password (the same one that gates engine
control and manual trades).
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request

from config.settings import get_settings
from config.trade_selection import (
    VALID_MIN_GRADES,
    VALID_STRATEGIES,
    TradeSelectionError,
    load_trade_selection,
    save_trade_selection,
)
from dashboard.auth import require_auth, verify_admin_password

router = APIRouter(prefix="/api/trade-selection", tags=["trade-selection"])


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


@router.post("")
async def set_selection(request: Request, _user: str = Depends(require_auth)):
    """Replace the selection (admin password required).

    Body: ``{enabled, symbols, strategies, min_grade, admin_password}``.
    """
    try:
        body: Dict[str, Any] = await request.json()
    except (ValueError, TypeError):
        body = {}
    if not isinstance(body, dict):
        body = {}

    if not verify_admin_password(str(body.get("admin_password", ""))):
        raise HTTPException(status_code=403, detail="Admin password required.")

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
