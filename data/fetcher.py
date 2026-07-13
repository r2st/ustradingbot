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


def fetch_ohlcv(
    symbol: str, period: str = "6mo", interval: str = "1d"
) -> Optional[pd.DataFrame]:
    """Fetch OHLCV bars for a single symbol (cached + retried).

    Args:
        symbol: Ticker symbol (e.g. ``"AAPL"``, ``"SHOP.TO"``).
        period: Look-back window (provider-specific; yfinance semantics).
        interval: Bar grain — ``"1d"`` (default, daily), intraday
            (``"1h"``/``"15m"``/``"5m"``/``"1m"``), or coarser
            (``"1wk"``/``"1mo"``).  Each grain is cached separately.

    Returns:
        A :class:`pandas.DataFrame` with a :class:`pandas.DatetimeIndex` and
        columns ``[Open, High, Low, Close, Volume]``, or ``None`` on failure.
    """
    settings = get_settings()
    key = ("ohlcv", symbol, period, interval)

    if settings.DATA_CACHE_ENABLED:
        cached = _cache.get(key)
        if cached is not _MISS:
            logger.debug(
                "fetch_ohlcv.cache_hit", symbol=symbol, period=period, interval=interval
            )
            return cached  # type: ignore[return-value]

    provider = get_provider()
    result = _with_retry(
        lambda: provider.get_ohlcv(symbol, period, interval),
        what="ohlcv",
        symbol=symbol,
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
    interval: str = "1d",
) -> Dict[str, pd.DataFrame]:
    """Batch-fetch OHLCV data for a list of symbols.

    Symbols that fail to download are skipped (logged at warning level) so a
    single delisted ticker never blocks a scan cycle.

    Args:
        symbols: List of ticker symbols to fetch.
        period: Look-back window (same semantics as :func:`fetch_ohlcv`).
        interval: Bar grain (same semantics as :func:`fetch_ohlcv`).

    Returns:
        A dict mapping each successfully fetched symbol to its DataFrame.
    """
    results: Dict[str, pd.DataFrame] = {}

    for symbol in symbols:
        df = fetch_ohlcv(symbol, period=period, interval=interval)
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


# ---------------------------------------------------------------------------
# Batch OHLCV download (Full Stock Universe optimisation)
# ---------------------------------------------------------------------------


def fetch_batch_ohlcv(
    symbols: List[str],
    period: str = "2y",
    batch_size: int = 50,
    delay_between_batches: float = 1.0,
    interval: str = "1d",
) -> Dict[str, pd.DataFrame]:
    """Download OHLCV data for many symbols using yfinance batch download.

    Partitions *symbols* into batches of *batch_size* and calls
    ``yf.download()`` (or the provider equivalent) with the whole batch at
    once.  This is dramatically more efficient than per-symbol requests when
    scanning thousands of tickers — the Full Stock Universe expansion can
    fetch 7,000 symbols in ~140 batch calls instead of 7,000 serial ones.

    Results are stored in the module cache so subsequent per-symbol calls
    (e.g. from :func:`fetch_ohlcv`) get a cache hit.

    Args:
        symbols: Tickers to download (duplicates ignored).
        period: Look-back window (yfinance semantics, e.g. ``"2y"``).
        batch_size: Max symbols per ``yf.download()`` call.
        delay_between_batches: Seconds to wait between batch calls to
            avoid rate-limiting (adaptive: doubles on empty results).

    Returns:
        Dict mapping each successfully fetched symbol to its DataFrame.
    """
    import math

    settings = get_settings()
    unique = list(dict.fromkeys(symbols))  # deduplicate, preserve order
    results: Dict[str, pd.DataFrame] = {}

    # Check cache first and build the uncached list.
    uncached: List[str] = []
    for sym in unique:
        key = ("ohlcv", sym, period, interval)
        if settings.DATA_CACHE_ENABLED:
            cached = _cache.get(key)
            if cached is not _MISS:
                results[sym] = cached  # type: ignore[assignment]
                continue
        uncached.append(sym)

    if not uncached:
        logger.debug("fetch_batch_ohlcv.all_cached", count=len(results))
        return results

    n_batches = math.ceil(len(uncached) / batch_size)
    logger.info(
        "fetch_batch_ohlcv.start",
        total=len(uncached),
        batch_size=batch_size,
        n_batches=n_batches,
    )

    current_delay = delay_between_batches
    empty_streak = 0

    for i in range(0, len(uncached), batch_size):
        batch = uncached[i: i + batch_size]
        batch_results = _download_batch(batch, period, interval)

        if not batch_results:
            empty_streak += 1
            if empty_streak >= 3:
                # Adaptive backoff: triple the delay after 3 consecutive empties.
                current_delay = min(current_delay * 3, 30.0)
                logger.warning(
                    "fetch_batch_ohlcv.adaptive_backoff",
                    delay=current_delay,
                    empty_streak=empty_streak,
                )
        else:
            empty_streak = 0
            current_delay = delay_between_batches

        for sym, df in batch_results.items():
            results[sym] = df
            if settings.DATA_CACHE_ENABLED:
                _cache.set(
                    ("ohlcv", sym, period, interval),
                    df,
                    settings.OHLCV_CACHE_TTL_SECONDS,
                )

        # Rate-limit between batches.
        if i + batch_size < len(uncached):
            time.sleep(current_delay)

    logger.info(
        "fetch_batch_ohlcv.complete",
        requested=len(uncached),
        succeeded=len(results) - (len(unique) - len(uncached)),
        cached=len(unique) - len(uncached),
    )
    return results


def _download_batch(
    symbols: List[str], period: str, interval: str = "1d"
) -> Dict[str, pd.DataFrame]:
    """Download OHLCV for a batch of symbols using yf.download().

    Returns a dict of symbol -> DataFrame.  Empty on any error.
    """
    try:
        import yfinance as yf

        from data.providers import _yf_interval, clamp_period_for_interval, normalize_interval

        interval = normalize_interval(interval)
        data = yf.download(
            tickers=symbols,
            period=clamp_period_for_interval(period, interval),
            interval=_yf_interval(interval),
            group_by="ticker",
            threads=True,
            progress=False,
        )
        if data is None or data.empty:
            return {}

        results: Dict[str, pd.DataFrame] = {}
        if len(symbols) == 1:
            # Single symbol: yf.download returns a flat DataFrame.
            sym = symbols[0]
            df = data.copy()
            if not df.empty and len(df) > 0:
                # Normalise column names — multi-level columns from yf.download
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.droplevel(0)
                df = df.rename(columns=str.title)
                needed = {"Open", "High", "Low", "Close", "Volume"}
                if needed.issubset(set(df.columns)):
                    results[sym] = df[list(needed)]
        else:
            # Multiple symbols: data has MultiIndex columns (ticker, field).
            for sym in symbols:
                try:
                    if sym in data.columns.get_level_values(0):
                        df = data[sym].copy()
                    else:
                        # Try case-insensitive match.
                        found = [
                            c for c in data.columns.get_level_values(0).unique()
                            if str(c).upper() == sym.upper()
                        ]
                        if not found:
                            continue
                        df = data[found[0]].copy()
                    df = df.dropna(how="all")
                    if df.empty:
                        continue
                    df = df.rename(columns=str.title)
                    needed = {"Open", "High", "Low", "Close", "Volume"}
                    if needed.issubset(set(df.columns)):
                        results[sym] = df[list(needed)]
                except Exception:  # noqa: BLE001
                    continue
        return results
    except Exception:
        logger.exception("_download_batch.error", symbols=len(symbols))
        return {}
