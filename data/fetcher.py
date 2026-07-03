"""
Market data fetcher for the US Trading Bot.

Retrieves OHLCV (Open, High, Low, Close, Volume) price data from Yahoo
Finance via the ``yfinance`` library.  All public functions return ``None``
on failure so callers can skip symbols gracefully without try/except
boilerplate.

Typical usage::

    from data.fetcher import fetch_ohlcv, fetch_current_price, fetch_multiple

    df = fetch_ohlcv("AAPL")
    if df is not None:
        # df has a DatetimeIndex and columns [Open, High, Low, Close, Volume]
        ...

    price = fetch_current_price("AAPL")

    batch = fetch_multiple(["AAPL", "MSFT", "GOOG"])
"""

from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd
import structlog
import yfinance as yf

logger = structlog.get_logger(__name__)

# Minimum number of rows required for the longest indicator (EMA-200).
# If a fetch returns fewer rows, we log a warning so upstream callers
# can decide whether to proceed with degraded indicator coverage.
_MIN_ROWS_FOR_EMA200 = 200


def fetch_ohlcv(
    symbol: str,
    period: str = "6mo",
) -> Optional[pd.DataFrame]:
    """Fetch daily OHLCV bars for a single symbol from Yahoo Finance.

    Args:
        symbol: Ticker symbol (e.g. ``"AAPL"``, ``"SHOP.TO"``).
        period: Look-back window accepted by ``yfinance.Ticker.history``.
            The default ``"6mo"`` yields roughly 126 trading days.

    Returns:
        A :class:`pandas.DataFrame` with a :class:`pandas.DatetimeIndex`
        and columns ``[Open, High, Low, Close, Volume]``, or ``None``
        if the data could not be retrieved or was empty.

    Notes:
        - NaN rows are dropped so downstream indicators receive clean data.
        - If fewer than :data:`_MIN_ROWS_FOR_EMA200` rows are returned, a
          warning is logged.  The data is still returned -- the caller
          decides whether to reject the symbol.
        - Network errors, delisted symbols, and empty responses are all
          caught and logged; ``None`` is returned.
    """
    log = logger.bind(symbol=symbol, period=period)

    try:
        ticker = yf.Ticker(symbol)
        df: pd.DataFrame = ticker.history(period=period, auto_adjust=True)

        if df is None or df.empty:
            log.warning("fetch_ohlcv.empty_data", reason="no rows returned")
            return None

        # Keep only the standard OHLCV columns (yfinance may include
        # Dividends, Stock Splits, Capital Gains, etc.).
        expected_cols = ["Open", "High", "Low", "Close", "Volume"]
        missing = [c for c in expected_cols if c not in df.columns]
        if missing:
            log.warning("fetch_ohlcv.missing_columns", missing=missing)
            return None

        df = df[expected_cols].copy()

        # Drop rows with any NaN -- partial bars would corrupt indicators.
        rows_before = len(df)
        df.dropna(inplace=True)
        rows_dropped = rows_before - len(df)
        if rows_dropped > 0:
            log.debug(
                "fetch_ohlcv.dropped_nan_rows",
                rows_dropped=rows_dropped,
                rows_remaining=len(df),
            )

        if df.empty:
            log.warning("fetch_ohlcv.all_nan", reason="all rows were NaN")
            return None

        # Ensure the index is a proper DatetimeIndex (yfinance usually
        # provides this, but belt-and-suspenders).
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)

        if len(df) < _MIN_ROWS_FOR_EMA200:
            log.warning(
                "fetch_ohlcv.insufficient_rows",
                rows=len(df),
                minimum=_MIN_ROWS_FOR_EMA200,
                hint="EMA-200 and other long-period indicators may be unreliable",
            )

        log.debug("fetch_ohlcv.success", rows=len(df))
        return df

    except Exception:
        log.exception("fetch_ohlcv.error")
        return None


def fetch_current_price(symbol: str) -> Optional[float]:
    """Get the latest available price for a symbol.

    Attempts to read the ``regularMarketPrice`` from Yahoo Finance fast-info.
    Falls back to the last ``Close`` value in the daily history if fast-info
    is unavailable.

    Args:
        symbol: Ticker symbol.

    Returns:
        The latest price as a float, or ``None`` on failure.
    """
    log = logger.bind(symbol=symbol)

    try:
        ticker = yf.Ticker(symbol)

        # fast_info is the quickest path -- avoids downloading full history.
        try:
            price = ticker.fast_info.get("lastPrice")
            if price is not None and price > 0:
                log.debug("fetch_current_price.fast_info", price=price)
                return float(price)
        except (AttributeError, KeyError):
            pass

        # Fallback: pull the most recent close from a short history window.
        df = ticker.history(period="5d", auto_adjust=True)
        if df is not None and not df.empty and "Close" in df.columns:
            price = float(df["Close"].dropna().iloc[-1])
            log.debug("fetch_current_price.history_fallback", price=price)
            return price

        log.warning("fetch_current_price.unavailable")
        return None

    except Exception:
        log.exception("fetch_current_price.error")
        return None


def fetch_multiple(
    symbols: List[str],
    period: str = "6mo",
) -> Dict[str, pd.DataFrame]:
    """Batch-fetch OHLCV data for a list of symbols.

    Symbols that fail to download are silently skipped (logged at warning
    level).  This is intentional: a single delisted ticker should not
    block the entire scan cycle.

    Args:
        symbols: List of ticker symbols to fetch.
        period: Look-back window (same semantics as :func:`fetch_ohlcv`).

    Returns:
        A dict mapping each successfully fetched symbol to its
        :class:`pandas.DataFrame`.  May be empty if every symbol fails.
    """
    results: Dict[str, pd.DataFrame] = {}

    for symbol in symbols:
        df = fetch_ohlcv(symbol, period=period)
        if df is not None:
            results[symbol] = df
        else:
            logger.warning(
                "fetch_multiple.skipped",
                symbol=symbol,
                reason="fetch returned None",
            )

    logger.info(
        "fetch_multiple.complete",
        requested=len(symbols),
        succeeded=len(results),
        failed=len(symbols) - len(results),
    )
    return results
