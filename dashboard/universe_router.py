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

from fastapi import APIRouter, Depends, HTTPException

from config.settings import EASTERN, get_settings
from dashboard.auth import require_auth
from dashboard.schemas import (
    UniverseAddWatchlistRequest,
    UniverseFilterRequest,
    UniverseRemoveWatchlistRequest,
    UniverseSeedRequest,
    UniverseWatchlistEnabledRequest,
)
from data_store.universe import db_exists, get_universe_db

router = APIRouter(prefix="/api/universe", tags=["Configuration"])

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
async def add_to_watchlist(
    payload: UniverseAddWatchlistRequest, _user: str = Depends(require_auth)
):
    """Add symbols to a watchlist (creates it if needed).

    Body: ``{"list_name": "My List", "tickers": ["AAPL", "MSFT"]}``
    """
    db = _require_db()
    if db is None:
        return _not_available()
    list_name = payload.list_name.strip()
    tickers = payload.tickers
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
    list_name: str,
    payload: UniverseRemoveWatchlistRequest,
    _user: str = Depends(require_auth),
):
    """Remove specific symbols from a watchlist.

    Body: ``{"tickers": ["AAPL"]}``
    """
    db = _require_db()
    if db is None:
        return _not_available()
    tickers = payload.tickers
    if not isinstance(tickers, list) or not tickers:
        raise HTTPException(status_code=400, detail="'tickers' must be a non-empty list.")
    removed = db.remove_from_watchlist(list_name, tickers)
    return {"ok": True, "list_name": list_name, "removed": removed}


@router.post("/watchlists/{list_name}/enabled")
async def set_watchlist_enabled(
    list_name: str,
    payload: UniverseWatchlistEnabledRequest,
    _user: str = Depends(require_auth),
):
    """Enable or disable a watchlist for scanning.

    Body: ``{"enabled": true}``
    """
    db = _require_db()
    if db is None:
        return _not_available()
    enabled = bool(payload.enabled)
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
async def set_filter(
    payload: UniverseFilterRequest, _user: str = Depends(require_auth)
):
    """Set or update a scan filter.

    Body: ``{"filter_name": "min_price", "filter_value": 10.0, "enabled": true}``
    """
    db = _require_db()
    if db is None:
        return _not_available()
    name = payload.filter_name.strip()
    value = payload.filter_value
    enabled = payload.enabled
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
    """Index-based tier breakdown for the Universe Browser.

    Reflects the operator-approved tier model (see :mod:`config.universe`):

    * **Tier 1 (Active Trading)** — watchlist ∪ ETFs ∪ auto-promoted symbols,
      scanned every cycle.
    * **Tier 2 (Scan Pool)** — top-N S&P 500 by volume × market cap, daily.
    * **Tier 3 (Universe)** — full S&P 500 ∪ NASDAQ-100, weekly.

    Also echoes the two settings that drive the tiers so the UI can display
    them: ``TIER2_SCAN_POOL_SIZE`` and ``PROMOTION_TTL_HOURS``.
    """
    db = _require_db()
    if db is None:
        return _not_available()

    from config.etf_universe import ALL_ETFS
    from config.index_membership import (
        NASDAQ100_INDEX,
        SP500_INDEX,
        nasdaq100_symbols,
        sp500_symbols,
    )

    settings = _settings()
    pool_size = int(getattr(settings, "TIER2_SCAN_POOL_SIZE", 250))
    ttl_hours = float(getattr(settings, "PROMOTION_TTL_HOURS", 72.0))

    # ── Tier 1: watchlist ∪ ETFs ∪ promoted ──
    watchlist = [t.upper() for t in db.get_tier1_symbols()]
    etfs = [t.upper() for t in ALL_ETFS]
    promoted = [t.upper() for t in db.get_promoted_symbols()]
    tier1_set = set(watchlist) | set(etfs) | set(promoted)

    # ── Tier 2: scan pool (top-N S&P 500 by liquidity) ──
    scan_pool = db.get_scan_pool(index_name=SP500_INDEX, limit=pool_size)

    # ── Tier 3: index universe (S&P 500 ∪ NASDAQ-100) ──
    tier3 = db.get_index_universe()
    sp500_members = db.get_index_symbols(SP500_INDEX) or sp500_symbols()
    nasdaq_members = db.get_index_symbols(NASDAQ100_INDEX) or nasdaq100_symbols()
    if not tier3:  # DB has no recorded membership yet — fall back to static union
        tier3 = sorted(set(sp500_members) | set(nasdaq_members))

    return {
        "available": True,
        "tier1": {
            "count": len(tier1_set),
            "watchlist_count": len(set(watchlist)),
            "etf_count": len(set(etfs)),
            "promoted_count": len(set(promoted)),
            "symbols": sorted(tier1_set),
        },
        "tier2": {
            "count": len(scan_pool),
            "pool_size": pool_size,
            "index": SP500_INDEX,
            "ranking": "volume × market cap",
        },
        "tier3": {
            "count": len(tier3),
            "sp500_count": len(sp500_members),
            "nasdaq100_count": len(nasdaq_members),
        },
        "settings": {
            "TIER2_SCAN_POOL_SIZE": pool_size,
            "PROMOTION_TTL_HOURS": ttl_hours,
        },
    }


@router.get("/indices")
async def get_indices(_user: str = Depends(require_auth)):
    """S&P 500 and NASDAQ-100 membership with a per-symbol index indicator.

    Prefers the DB-recorded membership (kept current by the seeder's refresh)
    and falls back to the curated static lists in
    :mod:`config.index_membership` so the view is populated even with no DB.

    Returns a merged, sorted symbol list where each entry flags which indices
    it belongs to, plus per-index counts and the size of the union/overlap.
    """
    db = _require_db()
    if db is None:
        return _not_available()

    from config.index_membership import (
        NASDAQ100_INDEX,
        SP500_INDEX,
        nasdaq100_symbols,
        sp500_symbols,
    )

    sp500 = set(t.upper() for t in (db.get_index_symbols(SP500_INDEX) or sp500_symbols()))
    nasdaq = set(
        t.upper() for t in (db.get_index_symbols(NASDAQ100_INDEX) or nasdaq100_symbols())
    )
    both = sp500 & nasdaq

    symbols = [
        {
            "ticker": t,
            "sp500": t in sp500,
            "nasdaq100": t in nasdaq,
        }
        for t in sorted(sp500 | nasdaq)
    ]
    return {
        "available": True,
        "symbols": symbols,
        "sp500_count": len(sp500),
        "nasdaq100_count": len(nasdaq),
        "both_count": len(both),
        "union_count": len(sp500 | nasdaq),
    }


@router.get("/scan-pool")
async def get_scan_pool(limit: Optional[int] = None, _user: str = Depends(require_auth)):
    """The Tier-2 Scan Pool ranked by liquidity, with the ranking metric shown.

    Ranked by ``avg_volume × market_cap`` descending.  Defaults to the
    configured ``TIER2_SCAN_POOL_SIZE`` when *limit* is omitted.
    """
    db = _require_db()
    if db is None:
        return _not_available()
    from config.index_membership import SP500_INDEX

    settings = _settings()
    pool_size = limit if limit is not None else int(
        getattr(settings, "TIER2_SCAN_POOL_SIZE", 250)
    )
    rows = db.get_scan_pool_ranked(index_name=SP500_INDEX, limit=pool_size)
    return {
        "available": True,
        "index": SP500_INDEX,
        "pool_size": pool_size,
        "ranking": "volume × market cap",
        "count": len(rows),
        "symbols": rows,
    }


@router.get("/promotions")
async def get_promotions(_user: str = Depends(require_auth)):
    """Currently promoted symbols (Tier 2/3 → Tier 1) with TTL remaining.

    Each promotion carries the source tier, the reason the signal fired, when
    it was promoted, and how much longer it stays in Tier 1 before reverting to
    its scan-pool cadence (``ttl_remaining_hours``; ``None`` = never expires).
    """
    db = _require_db()
    if db is None:
        return _not_available()

    # Clear any stragglers so the view never shows an expired promotion.
    db.expire_promotions()
    rows = db.get_promotions()
    now = datetime.now(tz=EASTERN)
    for row in rows:
        expires = row.get("expires_at")
        remaining = None
        if expires:
            try:
                exp_dt = datetime.fromisoformat(expires)
                remaining = round((exp_dt - now).total_seconds() / 3600.0, 2)
                if remaining < 0:
                    remaining = 0.0
            except (ValueError, TypeError):
                remaining = None
        row["ttl_remaining_hours"] = remaining
    return {"available": True, "promotions": rows, "count": len(rows)}


@router.post("/promotions/{ticker}/demote")
async def demote_promotion(ticker: str, _user: str = Depends(require_auth)):
    """Manually demote *ticker* out of Tier 1 (removes its promotion)."""
    db = _require_db()
    if db is None:
        return _not_available()
    db.demote_symbol(ticker)
    return {"ok": True, "demoted": ticker.upper()}


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
async def seed_universe(
    payload: Optional[UniverseSeedRequest] = None,
    _user: str = Depends(require_auth),
):
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

    skip_enrichment = bool(payload.skip_enrichment) if payload else False
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
