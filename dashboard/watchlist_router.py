"""
Dashboard API for managing watchlists (feature 1).

Exposes CRUD endpoints under ``/api/watchlist`` so the browser UI can add or
remove symbols, create/delete named lists, and enable/disable a whole list for
scanning.  Every route is guarded by the shared HTTP Basic Auth dependency.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request

from config.settings import get_settings
from config.watchlist import WatchlistError, get_watchlist_store
from dashboard.auth import require_auth

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


def _store():
    return get_watchlist_store(get_settings().DATA_DIR)


def _payload() -> Dict[str, Any]:
    store = _store()
    lists = store.as_dict()
    return {
        "lists": [
            {"name": name, "symbols": e["symbols"], "enabled": e["enabled"],
             "count": len(e["symbols"])}
            for name, e in sorted(lists.items())
        ],
        "scan_symbols": store.scan_symbols(),
        "scan_count": len(store.scan_symbols()),
    }


async def _body(request: Request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


@router.get("")
async def get_watchlists(_user: str = Depends(require_auth)):
    """Return every list, its symbols, and the effective scan set."""
    return _payload()


@router.post("/lists")
async def create_list(request: Request, _user: str = Depends(require_auth)):
    body = await _body(request)
    try:
        _store().create_list(str(body.get("name", "")))
    except WatchlistError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _payload()


@router.delete("/lists/{name}")
async def delete_list(name: str, _user: str = Depends(require_auth)):
    try:
        _store().delete_list(name)
    except WatchlistError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _payload()


@router.post("/lists/{name}/enabled")
async def set_enabled(name: str, request: Request, _user: str = Depends(require_auth)):
    body = await _body(request)
    try:
        _store().set_enabled(name, bool(body.get("enabled", True)))
    except WatchlistError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _payload()


@router.post("/symbols")
async def add_symbol(request: Request, _user: str = Depends(require_auth)):
    body = await _body(request)
    name = str(body.get("list", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="A target list is required.")
    try:
        _store().add_symbol(name, str(body.get("symbol", "")))
    except WatchlistError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _payload()


@router.delete("/lists/{name}/symbols/{symbol}")
async def remove_symbol(name: str, symbol: str, _user: str = Depends(require_auth)):
    try:
        _store().remove_symbol(name, symbol)
    except WatchlistError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _payload()
