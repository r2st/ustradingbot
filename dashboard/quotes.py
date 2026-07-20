"""
Shared quote service for the dashboard (monitoring feature 1).

Every dashboard feature that needs a *current* market price — live P&L,
position monitoring, watchlist monitor, mark-to-market risk — goes through
this one module instead of calling a provider directly.  It serves quotes
from a short-TTL cache (``QUOTE_CACHE_TTL_SECONDS``, default 15 s) so a
browser polling several sections at once never turns into a provider call
per poll per symbol; misses are fetched through :mod:`data.fetcher`, which
adds its own longer-lived cache, retries, and the provider fallback chain.

Each quote is ``{"price", "prev_close", "change_pct", "fetched_at"}``.  A
symbol whose fetch fails yields ``price: None`` (callers render "—" and skip
it in totals) — one bad symbol never fails a whole response.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog

log = structlog.get_logger(__name__)

# Indirection points so tests can monkeypatch without touching data.fetcher.
def _fetch_price(symbol: str) -> Optional[float]:
    from data.fetcher import fetch_current_price

    return fetch_current_price(symbol)


def _fetch_ohlcv(symbol: str):
    from data.fetcher import fetch_ohlcv

    return fetch_ohlcv(symbol)


_lock = threading.Lock()
# symbol -> (quote_dict, monotonic_expiry)
_cache: Dict[str, tuple] = {}
# prev-close is stable for a whole session; cache it much longer (1 h).
_PREV_CLOSE_TTL = 3600.0
_prev_close_cache: Dict[str, tuple] = {}


def clear_cache() -> None:
    """Empty the quote cache (tests / manual refresh)."""
    with _lock:
        _cache.clear()
        _prev_close_cache.clear()


def _ttl() -> float:
    try:
        from dashboard.auth import get_settings

        return float(get_settings().QUOTE_CACHE_TTL_SECONDS)
    except Exception:  # noqa: BLE001 -- default keeps the service usable
        return 15.0


def _prev_close(symbol: str) -> Optional[float]:
    """Previous session close for *symbol* (day-change baseline), cached 1 h."""
    now = time.monotonic()
    with _lock:
        hit = _prev_close_cache.get(symbol)
        if hit is not None and now < hit[1]:
            return hit[0]
    value: Optional[float] = None
    try:
        df = _fetch_ohlcv(symbol)
        if df is not None and len(df) >= 2 and "Close" in df:
            value = float(df["Close"].iloc[-2])
    except Exception as exc:  # noqa: BLE001 -- prev-close is a nice-to-have
        log.debug("quotes.prev_close_failed", symbol=symbol, error=str(exc))
        value = None
    with _lock:
        _prev_close_cache[symbol] = (value, now + _PREV_CLOSE_TTL)
    return value


def get_quote(symbol: str, include_prev_close: bool = False) -> Dict[str, Any]:
    """Return the cached quote for one symbol, fetching on a miss."""
    return get_quotes([symbol], include_prev_close=include_prev_close)[symbol]


def get_quotes(
    symbols: List[str],
    include_prev_close: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Return quotes for *symbols*, serving from the TTL cache where possible.

    Args:
        symbols: Ticker symbols (deduplicated; order preserved in the result).
        include_prev_close: Also resolve the previous close and day-change
            percentage (needs OHLCV history; watchlist monitor wants this,
            the P&L view does not).

    Returns:
        ``{symbol: {"price", "prev_close", "change_pct", "fetched_at"}}``.
        ``price`` is ``None`` when the fetch failed (callers must treat the
        symbol as stale, never as zero).
    """
    ttl = _ttl()
    now = time.monotonic()
    out: Dict[str, Dict[str, Any]] = {}
    misses: List[str] = []

    with _lock:
        for sym in dict.fromkeys(symbols):  # dedupe, keep order
            hit = _cache.get(sym)
            if hit is not None and now < hit[1]:
                out[sym] = dict(hit[0])
            else:
                misses.append(sym)

    for sym in misses:
        try:
            price = _fetch_price(sym)
        except Exception:  # noqa: BLE001 -- a bad symbol must not poison the batch
            log.warning("quotes.fetch_failed", symbol=sym)
            price = None
        quote = {
            "price": float(price) if price is not None else None,
            "prev_close": None,
            "change_pct": None,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        with _lock:
            # Cache failures briefly too, so a dead symbol is not re-fetched
            # on every poll tick.
            _cache[sym] = (quote, time.monotonic() + ttl)
        out[sym] = dict(quote)

    if include_prev_close:
        for sym, quote in out.items():
            if quote.get("prev_close") is None:
                pc = _prev_close(sym)
                quote["prev_close"] = pc
                price = quote.get("price")
                if pc and price is not None and pc > 0:
                    quote["change_pct"] = round((price - pc) / pc * 100.0, 2)
                # Backfill the cached entry so the next poll gets it for free.
                with _lock:
                    hit = _cache.get(sym)
                    if hit is not None:
                        hit[0]["prev_close"] = quote["prev_close"]
                        hit[0]["change_pct"] = quote["change_pct"]

    return out
