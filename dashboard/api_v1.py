"""
Documented REST API v1 (feature 19).

A read-only JSON API under ``/api/v1`` authenticated with an API key (minted from
the dashboard).  The key is presented either as ``Authorization: Bearer <key>``
or an ``X-API-Key: <key>`` header.  Endpoints cover positions, trades,
watchlist, and analytics.

Key *management* endpoints (create/list/revoke) live under ``/api/v1/keys`` and
are gated by the dashboard's HTTP Basic admin auth instead of an API key, so a
first key can be minted.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException

from config.settings import get_settings
from dashboard.api_keys import get_api_key_store
from dashboard.auth import require_auth
from dashboard.schemas import CreateApiKeyRequest

router = APIRouter(prefix="/api/v1", tags=["rest-api-v1"])


# ---------------------------------------------------------------------------
# API-key auth dependency
# ---------------------------------------------------------------------------


def require_api_key(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> str:
    """Authenticate a request via Bearer token or ``X-API-Key`` header."""
    settings = get_settings()
    if not getattr(settings, "REST_API_ENABLED", True):
        raise HTTPException(status_code=404, detail="REST API disabled")

    key = ""
    if authorization and authorization.lower().startswith("bearer "):
        key = authorization[7:].strip()
    elif x_api_key:
        key = x_api_key.strip()

    if not key or not get_api_key_store(settings.DATA_DIR).verify(key):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return key


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@router.get("")
async def api_index():
    """Public index documenting the available endpoints."""
    return {
        "name": "US Trading Bot REST API",
        "version": "1",
        "auth": "API key via 'Authorization: Bearer <key>' or 'X-API-Key: <key>'",
        "endpoints": [
            "/api/v1/positions",
            "/api/v1/trades",
            "/api/v1/watchlist",
            "/api/v1/analytics",
            "/api/v1/risk",
        ],
    }


# ---------------------------------------------------------------------------
# Data endpoints (API-key auth)
# ---------------------------------------------------------------------------


@router.get("/positions")
async def api_positions(_key: str = Depends(require_api_key)):
    from dashboard.app import _build_paper_trading

    paper = _build_paper_trading()
    return {"open_count": paper["open_count"], "positions": paper["positions"]}


@router.get("/trades")
async def api_trades(limit: int = 50, _key: str = Depends(require_api_key)):
    from dashboard.app import _analytics_report

    trades = _analytics_report().recent_trades
    return {"count": len(trades), "trades": trades[: max(1, min(limit, 500))]}


@router.get("/watchlist")
async def api_watchlist(_key: str = Depends(require_api_key)):
    from config.watchlist import get_watchlist_store

    store = get_watchlist_store(get_settings().DATA_DIR)
    return {
        "lists": store.as_dict(),
        "scan_symbols": store.scan_symbols(),
    }


@router.get("/analytics")
async def api_analytics(_key: str = Depends(require_api_key)):
    from dashboard.app import _analytics_report

    return _analytics_report().to_dict()


@router.get("/risk")
async def api_risk(_key: str = Depends(require_api_key)):
    from dashboard.app import _risk_report

    return _risk_report().to_dict()


# ---------------------------------------------------------------------------
# Key management (admin HTTP Basic auth)
# ---------------------------------------------------------------------------


@router.get("/keys")
async def list_keys(_user: str = Depends(require_auth)):
    store = get_api_key_store(get_settings().DATA_DIR)
    return {"keys": [k.to_dict() for k in store.list_keys()]}


@router.post("/keys")
async def create_key(
    payload: CreateApiKeyRequest, _user: str = Depends(require_auth)
):
    store = get_api_key_store(get_settings().DATA_DIR)
    raw, info = store.create(payload.name)
    # The raw key is returned exactly once.
    return {"key": raw, "info": info.to_dict()}


@router.delete("/keys/{key_id}")
async def revoke_key(key_id: str, _user: str = Depends(require_auth)):
    store = get_api_key_store(get_settings().DATA_DIR)
    if not store.revoke(key_id):
        raise HTTPException(status_code=404, detail="Unknown key id.")
    return {"ok": True, "keys": [k.to_dict() for k in store.list_keys()]}
