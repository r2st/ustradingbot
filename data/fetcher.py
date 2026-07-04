"""
Market data fetcher for the US Trading Bot.

Retrieves OHLCV (Open, High, Low, Close, Volume) price data and current
prices through the configured :class:`~data.providers.MarketDataProvider`
(Yahoo Finance by default; Alpaca when ``MARKET_DATA_PROVIDER=alpaca``).

Two cross-cutting concerns are handled here so every caller benefits:

* **Thread-safe TTL caching** — results are memoised per ``(symbol, period)``
  with a configurable time-to-live (5 min for prices, 1 h for OHLCV by
  default).  A single scan cycle touches each symbol from several subsystems
  (screener, freshness check, exit manager, trailing stops); caching collapses
  those into one backend call and eliminates the ~160 redundant fetches per
  cycle.
* **Retry with exponential backoff** — transient backend failures (an
  exception *or* an empty/``None`` result) are retried up to
  ``FETCH_MAX_RETRIES`` times before giving up and returning ``None``.

All public functions return ``None`` on failure so callers can skip symbols
gracefully.  The public API (``fetch_ohlcv``, ``fetch_current_price``,
``fetch_multiple``) is unchanged from earlier revisions.

Typical usage::

    from data.fetcher import fetch_ohlcv, fetch_current_price, fetch_multiple

    df = fetch_ohlcv("AAPL")
    price = fetch_current_price("AAPL")
    batch = fetch_multiple(["AAPL", "MSFT", "GOOG"])
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd
import structlog

from config.settings import get_settings
from data.providers import MarketDataProvider, make_provider

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Thread-safe TTL cache
# ---------------------------------------------------------------------------

_MISS = object()


class _TTLCache:
    """A minimal thread-safe cache with per-entry time-to-live.

    Entries are only ever *read* under the lock alongside an expiry check, so
    stale entries are evicted lazily on access.  Values are stored as-is;
    callers must not mutate a cached DataFrame in place (fetch functions
    return the cached object directly for speed, and the indicator code treats
    frames as read-only).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: Dict[Tuple, Tuple[object, Optional[float]]] = {}

    def get(self, key: Tuple) -> object:
        now = time.monotonic()
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return _MISS
            value, expiry = item
            if expiry is not None and now >= expiry:
                del self._store[key]
                return _MISS
            return value

    def set(self, key: Tuple, value: object, ttl: float) -> None:
        expiry = time.monotonic() + ttl if ttl and ttl > 0 else None
        with self._lock:
            self._store[key] = (value, expiry)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:  # pragma: no cover - debugging aid
        with self._lock:
            return len(self._store)


# Module-level singletons, guarded by a lock for lazy construction.
_cache = _TTLCache()
_provider_lock = threading.Lock()
_provider: Optional[MarketDataProvider] = None


def get_provider() -> MarketDataProvider:
    """Return the process-wide market-data provider (built lazily)."""
    global _provider
    if _provider is None:
        with _provider_lock:
            if _provider is None:
                _provider = make_provider(get_settings())
    return _provider


def set_provider(provider: Optional[MarketDataProvider]) -> None:
    """Override the active provider (mainly for tests); ``None`` resets it."""
    global _provider
    with _provider_lock:
        _provider = provider


def clear_cache() -> None:
    """Empty the fetch cache (mainly for tests and manual refreshes)."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Retry with exponential backoff
# ---------------------------------------------------------------------------


def _with_retry(
    func: Callable[[], Optional[object]],
    *,
    what: str,
    symbol: str,
) -> Optional[object]:
    """Call *func* with exponential-backoff retry.

    A ``None`` result or a raised exception both count as a transient failure
    and trigger a retry (up to ``FETCH_MAX_RETRIES``).  Returns the first
    non-``None`` result, or ``None`` once retries are exhausted.
    """
    settings = get_settings()
    attempts = max(1, int(settings.FETCH_MAX_RETRIES))
    base = settings.FETCH_RETRY_BASE_DELAY_SECONDS
    cap = settings.FETCH_RETRY_MAX_DELAY_SECONDS

    last_error = "empty"
    for attempt in range(1, attempts + 1):
        try:
            result = func()
            if result is not None:
                return result
            last_error = "empty_result"
        except Exception as exc:  # noqa: BLE001 -- classify as transient
            last_error = str(exc)
            logger.warning(
                "fetch.attempt_error",
                what=what,
                symbol=symbol,
                attempt=attempt,
                error=last_error,
            )

        if attempt < attempts:
            delay = min(base * (2 ** (attempt - 1)), cap)
            time.sleep(delay)

    logger.error(
        "fetch.exhausted", what=what, symbol=symbol, attempts=attempts, error=last_error
    )
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_ohlcv(symbol: str, period: str = "6mo") -> Optional[pd.DataFrame]:
    """Fetch daily OHLCV bars for a single symbol (cached + retried).

    Args:
        symbol: Ticker symbol (e.g. ``"AAPL"``, ``"SHOP.TO"``).
        period: Look-back window (provider-specific; yfinance semantics).

    Returns:
        A :class:`pandas.DataFrame` with a :class:`pandas.DatetimeIndex` and
        columns ``[Open, High, Low, Close, Volume]``, or ``None`` on failure.
    """
    settings = get_settings()
    key = ("ohlcv", symbol, period)

    if settings.DATA_CACHE_ENABLED:
        cached = _cache.get(key)
        if cached is not _MISS:
            logger.debug("fetch_ohlcv.cache_hit", symbol=symbol, period=period)
            return cached  # type: ignore[return-value]

    provider = get_provider()
    result = _with_retry(
        lambda: provider.get_ohlcv(symbol, period), what="ohlcv", symbol=symbol
    )

    if result is not None and settings.DATA_CACHE_ENABLED:
        _cache.set(key, result, settings.OHLCV_CACHE_TTL_SECONDS)
    return result  # type: ignore[return-value]


def fetch_current_price(symbol: str) -> Optional[float]:
    """Get the latest available price for a symbol (cached + retried).

    Args:
        symbol: Ticker symbol.

    Returns:
        The latest price as a float, or ``None`` on failure.
    """
    settings = get_settings()
    key = ("price", symbol)

    if settings.DATA_CACHE_ENABLED:
        cached = _cache.get(key)
        if cached is not _MISS:
            logger.debug("fetch_current_price.cache_hit", symbol=symbol)
            return cached  # type: ignore[return-value]

    provider = get_provider()
    result = _with_retry(
        lambda: provider.get_current_price(symbol), what="price", symbol=symbol
    )

    if result is not None and settings.DATA_CACHE_ENABLED:
        _cache.set(key, float(result), settings.PRICE_CACHE_TTL_SECONDS)
    return result  # type: ignore[return-value]


def fetch_multiple(
    symbols: List[str],
    period: str = "6mo",
) -> Dict[str, pd.DataFrame]:
    """Batch-fetch OHLCV data for a list of symbols.

    Symbols that fail to download are skipped (logged at warning level) so a
    single delisted ticker never blocks a scan cycle.

    Args:
        symbols: List of ticker symbols to fetch.
        period: Look-back window (same semantics as :func:`fetch_ohlcv`).

    Returns:
        A dict mapping each successfully fetched symbol to its DataFrame.
    """
    results: Dict[str, pd.DataFrame] = {}

    for symbol in symbols:
        df = fetch_ohlcv(symbol, period=period)
        if df is not None:
            results[symbol] = df
        else:
            logger.warning("fetch_multiple.skipped", symbol=symbol)

    logger.info(
        "fetch_multiple.complete",
        requested=len(symbols),
        succeeded=len(results),
        failed=len(symbols) - len(results),
    )
    return results
