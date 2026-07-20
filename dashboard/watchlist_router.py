"""
Dashboard API for managing watchlists (feature 1).

Exposes CRUD endpoints under ``/api/watchlist`` so the browser UI can add or
remove symbols, create/delete named lists, and enable/disable a whole list for
scanning.  Every route is guarded by the shared HTTP Basic Auth dependency.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from config.settings import get_settings
from config.watchlist import WatchlistError, get_watchlist_store
from dashboard.auth import require_auth
from dashboard.schemas import AddSymbolRequest, CreateListRequest, ListEnabledRequest

router = APIRouter(prefix="/api/watchlist", tags=["Configuration"])


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


@router.get("")
async def get_watchlists(_user: str = Depends(require_auth)):
    """Return every list, its symbols, and the effective scan set."""
    return _payload()


@router.post("/lists")
async def create_list(payload: CreateListRequest, _user: str = Depends(require_auth)):
    try:
        _store().create_list(payload.name)
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
async def set_enabled(
    name: str, payload: ListEnabledRequest, _user: str = Depends(require_auth)
):
    try:
        _store().set_enabled(name, bool(payload.enabled))
    except WatchlistError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _payload()


@router.post("/symbols")
async def add_symbol(payload: AddSymbolRequest, _user: str = Depends(require_auth)):
    name = payload.list.strip()
    if not name:
        raise HTTPException(status_code=400, detail="A target list is required.")
    try:
        _store().add_symbol(name, payload.symbol)
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


# ---------------------------------------------------------------------------
# Watchlist monitor (monitoring feature 8) — live status per scanned symbol
# ---------------------------------------------------------------------------


@router.get("/monitor")
async def watchlist_monitor(_user: str = Depends(require_auth)):
    """Live view of every watchlist symbol: price, day change, and status.

    Status priority: ``held`` (already in the book) > ``signal`` (produced a
    signal in the latest scan) > ``near_entry`` (recently rejected only on
    freshness/price drift — the setup exists but price moved) > ``rejected``
    (any other recent gate rejection) > ``excluded`` (filtered out by Engine
    Trade Selection) > ``idle``.
    """
    import json as _json
    from datetime import datetime, timedelta

    from config.settings import EASTERN
    from pathlib import Path

    from config.trade_selection import load_trade_selection
    from config.watchlist import scan_symbols_for
    from dashboard import quotes
    from dashboard.auth import get_settings as _resolve_settings
    from journal.activity_log import read_last_scan
    from journal.btst_logger import RejectedSignalLogger

    settings = _resolve_settings()
    data_dir = Path(settings.DATA_DIR)

    store = get_watchlist_store(settings.DATA_DIR)
    lists_by_symbol: Dict[str, list] = {}
    for name, entry in store.as_dict().items():
        if not entry.get("enabled", True):
            continue
        for sym in entry.get("symbols", []):
            lists_by_symbol.setdefault(str(sym).upper(), []).append(name)

    all_symbols = [str(s).upper() for s in scan_symbols_for(settings)]
    selection = load_trade_selection(data_dir)
    scanned = {str(s).upper() for s in selection.filter_symbols(all_symbols)}
    universe = sorted(set(all_symbols) | set(lists_by_symbol))

    # Open book (held symbols).
    held: set = set()
    pos_path = data_dir / "open_positions.json"
    if pos_path.exists():
        try:
            data = _json.loads(pos_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                held = {str(k).upper() for k in data.keys()}
        except (ValueError, OSError):
            pass

    # Latest scan's signals + recent rejections (last one per symbol).
    last_scan = read_last_scan(data_dir)
    signal_by_symbol = {
        str(s.get("symbol", "")).upper(): s
        for s in (last_scan.get("signals") or [])
    }
    rejections = RejectedSignalLogger(str(data_dir)).get_recent_rejections(500)
    cutoff = datetime.now(tz=EASTERN) - timedelta(days=2)
    rejection_by_symbol: Dict[str, dict] = {}
    for rec in rejections:  # oldest → newest, so later entries win
        try:
            ts = datetime.fromisoformat(str(rec.get("timestamp", "")))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=EASTERN)
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            continue
        rejection_by_symbol[str(rec.get("symbol", "")).upper()] = rec

    quote_map = quotes.get_quotes(universe, include_prev_close=True)

    rows = []
    for sym in universe:
        quote = quote_map.get(sym, {})
        sig = signal_by_symbol.get(sym)
        rej = rejection_by_symbol.get(sym)
        if sym in held:
            status = "held"
        elif sig is not None:
            status = "signal"
        elif rej is not None and str(rej.get("reason", "")) == "freshness_check":
            status = "near_entry"
        elif rej is not None:
            status = "rejected"
        elif sym not in scanned:
            status = "excluded"
        else:
            status = "idle"
        rows.append({
            "symbol": sym,
            "lists": lists_by_symbol.get(sym, []),
            "price": quote.get("price"),
            "change_pct": quote.get("change_pct"),
            "stale": quote.get("price") is None,
            "signal": (
                {
                    "strategy": sig.get("strategy"),
                    "grade": sig.get("grade"),
                    "strength": sig.get("signal_strength"),
                    "entry": sig.get("entry"),
                    "stop": sig.get("stop"),
                    "target": sig.get("target"),
                }
                if sig is not None else None
            ),
            "last_rejection": (
                {
                    "gate": rej.get("reason"),
                    "ts": rej.get("timestamp"),
                    "detail": rej.get("detail"),
                }
                if rej is not None else None
            ),
            "status": status,
        })

    _order = {"signal": 0, "near_entry": 1, "held": 2, "rejected": 3,
              "idle": 4, "excluded": 5}
    rows.sort(key=lambda r: (
        _order.get(r["status"], 9),
        -(r["change_pct"] if r["change_pct"] is not None else -999),
    ))

    from datetime import timezone
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "last_scan_at": last_scan.get("ts"),
        "symbols": rows,
    }
