"""Login-free market overview and stock screener.

The free tier of DoAide Trade: anyone can open ``/market`` (index + sector ETF
moves) and ``/screener`` (a technical screen over the core US watchlist)
without signing in.  Both pages render server-side so they are indexable, and
both have a JSON twin under ``/api/public/`` for the in-page refresh.

Data comes through the same :mod:`data.fetcher` path the engine uses, behind a
module-level snapshot cache (``_SNAPSHOT_TTL``) so anonymous traffic can never
turn into one provider call per visitor.  A symbol whose fetch fails is simply
left out — one bad ticker never fails the page.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from config.etf_universe import BROAD_MARKET_ETFS, SECTOR_ETFS
from config.universe import US_WATCHLIST, get_sector
from dashboard import stock_insight

log = structlog.get_logger(__name__)

router = APIRouter(tags=["Public"])

INDEX_NAMES: Dict[str, str] = {
    "SPY": "S&P 500",
    "QQQ": "Nasdaq 100",
    "DIA": "Dow Jones 30",
    "IWM": "Russell 2000",
}

#: Snapshot lifetime.  Daily bars barely move intraday at this granularity and
#: the public pages are a preview, not a trading terminal.
_SNAPSHOT_TTL = 600.0

_lock = threading.Lock()
# key -> (payload, monotonic_expiry)
_snapshots: Dict[str, tuple] = {}


def clear_cache() -> None:
    """Drop cached snapshots (tests / manual refresh)."""
    with _lock:
        _snapshots.clear()


def _fetch_ohlcv(symbol: str):
    from data.fetcher import fetch_ohlcv

    return fetch_ohlcv(symbol, period="6mo", interval="1d")


def _cached(key: str, build: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    now = time.monotonic()
    with _lock:
        hit = _snapshots.get(key)
        if hit is not None and now < hit[1]:
            return hit[0]
    payload = build()
    with _lock:
        _snapshots[key] = (payload, time.monotonic() + _SNAPSHOT_TTL)
    return payload


def _rsi(close, period: int = 14) -> Optional[float]:
    """Wilder RSI of the last bar, or ``None`` with too little history."""
    if len(close) <= period:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    last_loss = float(loss.iloc[-1])
    if last_loss == 0:
        return 100.0
    rs = float(gain.iloc[-1]) / last_loss
    return round(100.0 - 100.0 / (1.0 + rs), 1)


def _pct(new: float, old: float) -> Optional[float]:
    return round((new - old) / old * 100.0, 2) if old else None


def _symbol_row(symbol: str) -> Optional[Dict[str, Any]]:
    """Price, day/1M change, RSI and trend flags for one symbol."""
    try:
        df = _fetch_ohlcv(symbol)
    except Exception:  # noqa: BLE001 -- one bad symbol must not fail the page
        log.warning("public_market.fetch_failed", symbol=symbol)
        return None
    if df is None or len(df) < 2 or "Close" not in df:
        return None
    close = df["Close"].dropna()
    if len(close) < 2:
        return None
    last = float(close.iloc[-1])
    month_ago = float(close.iloc[-22]) if len(close) >= 22 else float(close.iloc[0])
    sma50 = float(close.tail(50).mean()) if len(close) >= 50 else None
    high = float(close.tail(126).max())
    volume = None
    if "Volume" in df and len(df["Volume"].dropna()):
        volume = int(df["Volume"].dropna().iloc[-1])
    return {
        "symbol": symbol,
        "sector": get_sector(symbol),
        "price": round(last, 2),
        "change_pct": _pct(last, float(close.iloc[-2])),
        "month_pct": _pct(last, month_ago),
        "rsi": _rsi(close),
        "above_sma50": (last > sma50) if sma50 is not None else None,
        "off_high_pct": _pct(last, high),
        "volume": volume,
    }


def _rows(symbols: List[str]) -> List[Dict[str, Any]]:
    # Fetch in parallel so a cold cache costs one round-trip, not one per symbol.
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_symbol_row, symbols))
    return [row for row in results if row is not None]


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_market_overview() -> Dict[str, Any]:
    """Index + sector ETF moves, plus breadth across the core watchlist."""

    def build() -> Dict[str, Any]:
        indices = _rows(BROAD_MARKET_ETFS)
        for row in indices:
            row["name"] = INDEX_NAMES.get(row["symbol"], row["symbol"])
        sector_by_etf = {etf: sector for sector, etf in SECTOR_ETFS.items()}
        sectors = _rows(list(SECTOR_ETFS.values()))
        for row in sectors:
            row["name"] = sector_by_etf.get(row["symbol"], row["symbol"])
        sectors.sort(key=lambda r: r["change_pct"] if r["change_pct"] is not None else -1e9,
                     reverse=True)
        stocks = build_screener()["rows"]
        moved = [r for r in stocks if r["change_pct"] is not None]
        moved.sort(key=lambda r: r["change_pct"], reverse=True)
        return {
            "as_of": _stamp(),
            "indices": indices,
            "sectors": sectors,
            "gainers": moved[:5],
            "losers": list(reversed(moved[-5:])) if len(moved) > 5 else [],
            "breadth": {
                "advancers": sum(1 for r in moved if r["change_pct"] > 0),
                "decliners": sum(1 for r in moved if r["change_pct"] < 0),
                "total": len(moved),
            },
        }

    return _cached("market", build)


def build_screener() -> Dict[str, Any]:
    """Technical snapshot of every symbol in the core US watchlist."""

    def build() -> Dict[str, Any]:
        rows = _rows(list(US_WATCHLIST))
        return {
            "as_of": _stamp(),
            "rows": rows,
            "sectors": sorted({r["sector"] for r in rows}),
        }

    return _cached("screener", build)


def _templates():
    from dashboard.app import templates

    return templates


@router.get("/market", response_class=HTMLResponse, include_in_schema=False)
async def market_page(request: Request):
    data = await run_in_threadpool(build_market_overview)
    return _templates().TemplateResponse(request, "viral/market.html", context={"data": data})


@router.get("/screener", response_class=HTMLResponse, include_in_schema=False)
async def screener_page(request: Request):
    data = await run_in_threadpool(build_screener)
    return _templates().TemplateResponse(request, "viral/screener.html", context={"data": data})


@router.get("/api/public/market")
async def market_api():
    """Login-free market overview (cached ~10 minutes)."""
    return await run_in_threadpool(build_market_overview)


@router.get("/api/public/screener")
async def screener_api():
    """Login-free screener rows for the core US watchlist (cached ~10 minutes)."""
    return await run_in_threadpool(build_screener)


def _watchlist_symbol(symbol: str) -> str:
    """Normalise *symbol*; 404 unless it is one the public pages cover."""
    sym = symbol.strip().upper()
    if sym not in US_WATCHLIST:
        raise HTTPException(status_code=404, detail="Unknown symbol")
    return sym


def build_stock(symbol: str, *, with_ai: bool = True) -> Optional[Dict[str, Any]]:
    """One stock's screener row plus its plain-English read, or ``None``.

    ``with_ai=False`` only reads the AI summary cache, so the HTML page renders
    without waiting on Gemini; the page's script then fetches the JSON twin,
    which makes the call.
    """
    screener = build_screener()
    row = next((r for r in screener["rows"] if r["symbol"] == symbol), None)
    if row is None:
        return None
    if with_ai:
        from dashboard.auth import get_settings

        ai = stock_insight.ai_summary(row, get_settings())
    else:
        ai = stock_insight.cached_ai_summary(symbol)
    return {
        "as_of": screener["as_of"],
        "row": row,
        "summary": stock_insight.technical_summary(row),
        "ai_summary": ai,
        "peers": [s for s in US_WATCHLIST if s != symbol and get_sector(s) == row["sector"]][:6],
    }


@router.get("/stock/{symbol}", response_class=HTMLResponse, include_in_schema=False)
async def stock_page(request: Request, symbol: str):
    sym = _watchlist_symbol(symbol)
    data = await run_in_threadpool(lambda: build_stock(sym, with_ai=False))
    return _templates().TemplateResponse(
        request, "viral/stock.html", context={"symbol": sym, "data": data},
        status_code=200 if data else 503,
    )


@router.get("/api/public/stock/{symbol}")
async def stock_api(symbol: str):
    """Login-free technical read of one watchlist stock, with an AI summary when available."""
    sym = _watchlist_symbol(symbol)
    data = await run_in_threadpool(build_stock, sym)
    if data is None:
        raise HTTPException(status_code=503, detail="Market data temporarily unavailable")
    return data
