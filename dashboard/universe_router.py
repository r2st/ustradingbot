"""
Dashboard API for the universe browser feature.

Exposes query, watchlist, filter, and seeding endpoints under
``/api/universe`` so the browser UI can browse, search, and manage the full
symbol universe.  Every route is guarded by the shared HTTP Basic Auth
dependency.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from config.settings import get_settings
from dashboard.auth import require_auth
from data_store.universe import db_exists, get_universe_db

router = APIRouter(prefix="/api/universe", tags=["universe"])

_DB_FILENAME = "universe.db"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings():
    return get_settings()


def _db():
    """Return the :class:`UniverseDB` singleton for the configured data dir."""
    s = _settings()
    return get_universe_db(s.DATA_DIR)


def _require_db():
    """Return the DB or raise a dict signalling "not initialised"."""
    s = _settings()
    if not db_exists(s.DATA_DIR):
        return None
    return _db()


def _not_available() -> Dict[str, Any]:
    return {
        "available": False,
        "message": "Universe database not initialized. Run seeder first.",
    }


async def _body(request: Request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Symbol browsing
# ---------------------------------------------------------------------------


@router.get("/symbols")
async def get_symbols(
    sector: Optional[str] = None,
    exchange: Optional[str] = None,
    asset_type: Optional[str] = None,
    country: Optional[str] = None,
    search: Optional[str] = None,
    min_price: Optional[float] = None,
    min_volume: Optional[float] = None,
    min_market_cap: Optional[float] = None,
    limit: int = 50,
    offset: int = 0,
    _user: str = Depends(require_auth),
):
    """Paginated symbol list with total count."""
    db = _require_db()
    if db is None:
        return _not_available()
    rows = db.get_symbols(
        exchange=exchange,
        sector=sector,
        asset_type=asset_type,
        country=country,
        is_active=True,
        min_price=min_price,
        min_volume=min_volume,
        min_market_cap=min_market_cap,
        search=search,
        limit=limit,
        offset=offset,
    )
    # get_symbols without limit/offset to count total matches
    total = len(
        db.get_symbols(
            exchange=exchange,
            sector=sector,
            asset_type=asset_type,
            country=country,
            is_active=True,
            min_price=min_price,
            min_volume=min_volume,
            min_market_cap=min_market_cap,
            search=search,
        )
    )
    return {"symbols": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/sectors")
async def get_sectors(_user: str = Depends(require_auth)):
    """All sectors with symbol counts."""
    db = _require_db()
    if db is None:
        return _not_available()
    return {"sectors": db.get_sectors()}


@router.get("/exchanges")
async def get_exchanges(_user: str = Depends(require_auth)):
    """All exchanges with symbol counts."""
    db = _require_db()
    if db is None:
        return _not_available()
    return {"exchanges": db.get_exchanges()}


@router.get("/stats")
async def get_stats(_user: str = Depends(require_auth)):
    """Universe statistics."""
    db = _require_db()
    if db is None:
        return _not_available()
    return db.get_stats()


@router.get("/search")
async def search_symbols(
    q: str = "",
    limit: int = 20,
    _user: str = Depends(require_auth),
):
    """Quick search by ticker or name."""
    db = _require_db()
    if db is None:
        return _not_available()
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query parameter 'q' is required.")
    results = db.search_symbols(q.strip(), limit=limit)
    return {"results": results, "count": len(results)}


# ---------------------------------------------------------------------------
# Watchlists (universe-level)
# ---------------------------------------------------------------------------


@router.get("/watchlists")
async def get_watchlists(_user: str = Depends(require_auth)):
    """All watchlist names with counts and enabled status."""
    db = _require_db()
    if db is None:
        return _not_available()
    return {"watchlists": db.get_watchlist_names()}


@router.get("/watchlists/{list_name}")
async def get_watchlist(list_name: str, _user: str = Depends(require_auth)):
    """Symbols in a specific watchlist."""
    db = _require_db()
    if db is None:
        return _not_available()
    symbols = db.get_watchlist(list_name)
    return {"list_name": list_name, "symbols": symbols, "count": len(symbols)}


@router.post("/watchlists")
async def add_to_watchlist(request: Request, _user: str = Depends(require_auth)):
    """Add symbols to a watchlist (creates it if needed).

    Body: ``{"list_name": "My List", "tickers": ["AAPL", "MSFT"]}``
    """
    db = _require_db()
    if db is None:
        return _not_available()
    body = await _body(request)
    list_name = str(body.get("list_name", "")).strip()
    tickers = body.get("tickers", [])
    if not list_name:
        raise HTTPException(status_code=400, detail="'list_name' is required.")
    if not isinstance(tickers, list) or not tickers:
        raise HTTPException(status_code=400, detail="'tickers' must be a non-empty list.")
    added = db.add_to_watchlist(list_name, tickers)
    return {"ok": True, "list_name": list_name, "added": added}


@router.delete("/watchlists/{list_name}")
async def delete_watchlist(list_name: str, _user: str = Depends(require_auth)):
    """Delete an entire watchlist."""
    db = _require_db()
    if db is None:
        return _not_available()
    db.delete_watchlist(list_name)
    return {"ok": True, "deleted": list_name}


@router.post("/watchlists/{list_name}/remove")
async def remove_from_watchlist(
    list_name: str, request: Request, _user: str = Depends(require_auth),
):
    """Remove specific symbols from a watchlist.

    Body: ``{"tickers": ["AAPL"]}``
    """
    db = _require_db()
    if db is None:
        return _not_available()
    body = await _body(request)
    tickers = body.get("tickers", [])
    if not isinstance(tickers, list) or not tickers:
        raise HTTPException(status_code=400, detail="'tickers' must be a non-empty list.")
    removed = db.remove_from_watchlist(list_name, tickers)
    return {"ok": True, "list_name": list_name, "removed": removed}


@router.post("/watchlists/{list_name}/enabled")
async def set_watchlist_enabled(
    list_name: str, request: Request, _user: str = Depends(require_auth),
):
    """Enable or disable a watchlist for scanning.

    Body: ``{"enabled": true}``
    """
    db = _require_db()
    if db is None:
        return _not_available()
    body = await _body(request)
    enabled = bool(body.get("enabled", True))
    db.set_watchlist_enabled(list_name, enabled)
    return {"ok": True, "list_name": list_name, "enabled": enabled}


# ---------------------------------------------------------------------------
# Scan filters
# ---------------------------------------------------------------------------


@router.get("/filters")
async def get_filters(_user: str = Depends(require_auth)):
    """Current scan filters."""
    db = _require_db()
    if db is None:
        return _not_available()
    return {"filters": db.get_scan_filters()}


@router.post("/filters")
async def set_filter(request: Request, _user: str = Depends(require_auth)):
    """Set or update a scan filter.

    Body: ``{"filter_name": "min_price", "filter_value": 10.0, "enabled": true}``
    """
    db = _require_db()
    if db is None:
        return _not_available()
    body = await _body(request)
    name = str(body.get("filter_name", "")).strip()
    value = body.get("filter_value")
    enabled = body.get("enabled", True)
    if not name:
        raise HTTPException(status_code=400, detail="'filter_name' is required.")
    if value is None:
        raise HTTPException(status_code=400, detail="'filter_value' is required.")
    db.set_scan_filter(name, value, enabled)
    return {"ok": True, "filter_name": name, "filter_value": value, "enabled": enabled}


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------


@router.get("/tiers")
async def get_tiers(_user: str = Depends(require_auth)):
    """Current tier info: tier 1 count, tier 2 sector list, tier 3 count."""
    db = _require_db()
    if db is None:
        return _not_available()
    tier1 = db.get_tier1_symbols()
    sectors = db.get_sectors()
    tier2_sectors = {}
    for s in sectors:
        sector_name = s.get("sector") or s.get("name", "")
        if sector_name:
            tier2_sectors[sector_name] = len(db.get_tier2_symbols(sector_name))
    tier3 = db.get_tier3_symbols()
    return {
        "tier1_count": len(tier1),
        "tier1_symbols": tier1,
        "tier2_sectors": tier2_sectors,
        "tier3_count": len(tier3),
    }


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


@router.post("/seed")
async def seed_universe(request: Request, _user: str = Depends(require_auth)):
    """Trigger re-seeding of the universe database (runs in background thread).

    Body (optional): ``{"skip_enrichment": true}``
    """
    from pathlib import Path

    from data_store.universe_seeder import UniverseSeeder

    body = await _body(request)
    skip_enrichment = bool(body.get("skip_enrichment", False))
    settings = _settings()
    db_path = Path(settings.DATA_DIR) / _DB_FILENAME

    def _run_seed():
        try:
            seeder = UniverseSeeder(str(db_path))
            seeder.seed_all(skip_enrichment=skip_enrichment)
        except Exception:
            import structlog
            log = structlog.get_logger(__name__)
            log.exception("universe_seed_failed")

    thread = threading.Thread(target=_run_seed, daemon=True, name="universe-seeder")
    thread.start()

    return {
        "ok": True,
        "message": "Seeding started in background. Refresh stats to check progress.",
        "skip_enrichment": skip_enrichment,
    }
