"""
Dashboard API for the universe browser feature.

Exposes query, watchlist, filter, and seeding endpoints under
``/api/universe`` so the browser UI can browse, search, and manage the full
symbol universe.  Every route is guarded by the shared HTTP Basic Auth
dependency.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from config.settings import get_settings
from dashboard.auth import require_auth
from data_store.universe import db_exists, get_universe_db

router = APIRouter(prefix="/api/universe", tags=["universe"])

_DB_FILENAME = "universe.db"

# ---------------------------------------------------------------------------
# Seed job registry — tracks background seeding jobs so the UI can poll
# ---------------------------------------------------------------------------

#: Retain at most this many seed jobs (oldest evicted first).
_MAX_SEED_JOBS = 5

_seed_jobs: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_seed_lock = threading.Lock()


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
# Seeding — background job with progress tracking
# ---------------------------------------------------------------------------


def _seed_job_update(job_id: str, **fields: Any) -> None:
    """Update fields on a seed job record (thread-safe)."""
    with _seed_lock:
        job = _seed_jobs.get(job_id)
        if job is not None:
            job.update(fields)


def _is_seed_running() -> bool:
    """Return True if any seed job is currently running."""
    with _seed_lock:
        return any(j["state"] == "running" for j in _seed_jobs.values())


@router.post("/seed")
async def seed_universe(request: Request, _user: str = Depends(require_auth)):
    """Trigger re-seeding of the universe database (runs in background thread).

    Returns a ``job_id`` that the UI polls via
    ``GET /api/universe/seed/status/{job_id}`` for progress updates.

    Body (optional): ``{"skip_enrichment": true}``
    """
    from pathlib import Path

    # Prevent multiple concurrent seed jobs
    if _is_seed_running():
        return {
            "ok": False,
            "message": "A seed job is already running. Please wait for it to finish.",
        }

    body = await _body(request)
    skip_enrichment = bool(body.get("skip_enrichment", False))
    settings = _settings()
    db_path = Path(settings.DATA_DIR) / _DB_FILENAME

    job_id = uuid.uuid4().hex[:12]
    job: Dict[str, Any] = {
        "id": job_id,
        "state": "running",
        "progress": 0,
        "message": "Starting universe rebuild...",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "skip_enrichment": skip_enrichment,
    }
    with _seed_lock:
        _seed_jobs[job_id] = job
        while len(_seed_jobs) > _MAX_SEED_JOBS:
            _seed_jobs.popitem(last=False)

    def _run_seed():
        import structlog

        log = structlog.get_logger(__name__)
        try:
            from data_store.universe_seeder import UniverseSeeder

            seeder = UniverseSeeder(str(db_path))

            # Phase 1: Fetch SEC tickers (10%)
            _seed_job_update(job_id, progress=2, message="Fetching SEC EDGAR tickers...")
            sec_symbols = seeder.fetch_sec_tickers()

            if sec_symbols:
                _seed_job_update(job_id, progress=10, message=f"Adding {len(sec_symbols)} US symbols...")
                seeder.db.add_symbols(sec_symbols)
            else:
                _seed_job_update(job_id, progress=10, message="SEC fetch returned no symbols, continuing...")

            # Phase 2: Canadian stocks (15%)
            _seed_job_update(job_id, progress=15, message="Adding Canadian stocks...")
            seeder.seed_canadian()

            # Phase 3: ETFs (20%)
            _seed_job_update(job_id, progress=20, message="Adding ETFs...")
            seeder.seed_etfs()

            # Phase 4: Enrichment (20-90%) — the slow part
            if not skip_enrichment:
                _seed_job_update(job_id, progress=22, message="Starting yfinance enrichment...")
                all_symbols = seeder.db.get_symbols(is_active=False)
                if all_symbols:
                    all_tickers = [s["ticker"] for s in all_symbols]
                    total = len(all_tickers)
                    batch_size = 50
                    enriched = 0

                    for i in range(0, total, batch_size):
                        batch = all_tickers[i : i + batch_size]
                        pct = 22 + int((i / total) * 68)  # 22% to 90%
                        _seed_job_update(
                            job_id,
                            progress=min(pct, 90),
                            message=f"Enriching symbols {i + 1}-{min(i + batch_size, total)} of {total}...",
                        )
                        try:
                            enriched += seeder.enrich_batch(batch, batch_size=batch_size)
                        except Exception:
                            log.debug("enrich_batch_error", batch_start=i)
                else:
                    _seed_job_update(job_id, progress=90, message="No symbols to enrich.")
            else:
                _seed_job_update(job_id, progress=90, message="Enrichment skipped.")

            # Phase 5: Default filters (92%)
            _seed_job_update(job_id, progress=92, message="Setting default scan filters...")
            seeder.seed_default_filters()

            # Phase 6: Migrate watchlist (95%)
            _seed_job_update(job_id, progress=95, message="Migrating existing watchlist...")
            try:
                seeder.migrate_existing_watchlist()
            except Exception:
                log.exception("watchlist_migration_failed")

            # Done
            stats = seeder.db.get_stats()
            total_symbols = stats.get("total_symbols", "?")
            _seed_job_update(
                job_id,
                state="done",
                progress=100,
                message=f"Universe rebuilt successfully. {total_symbols} symbols loaded.",
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            log.info("universe_seed_complete", job_id=job_id, **stats)

        except Exception as exc:
            log.exception("universe_seed_failed", job_id=job_id)
            _seed_job_update(
                job_id,
                state="error",
                message=f"Seed failed: {exc}",
                finished_at=datetime.now(timezone.utc).isoformat(),
            )

    thread = threading.Thread(target=_run_seed, daemon=True, name=f"universe-seeder-{job_id}")
    thread.start()

    return {"ok": True, "job_id": job_id}


@router.get("/seed/status/{job_id}")
async def seed_status(job_id: str, _user: str = Depends(require_auth)):
    """Poll the progress of a seed job.

    Returns the job state (``running``, ``done``, or ``error``), a progress
    percentage (0-100), and a human-readable message.
    """
    with _seed_lock:
        job = _seed_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown or expired seed job.")
        return dict(job)
