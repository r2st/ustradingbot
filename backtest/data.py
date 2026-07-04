"""
Historical data loading for the backtester.

Fetches daily OHLCV bars over an explicit ``[start, end]`` window (plus a
warm-up buffer so indicators like EMA-200 have enough history *before* the
first trading day).  Yahoo Finance is used directly here — the backtester is
an offline batch tool, so it bypasses the live fetcher's TTL cache.

The main entry point, :func:`load_price_history`, returns a mapping of symbol
to a canonical OHLCV DataFrame.  Tests inject their own data instead of hitting
the network.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Union

import pandas as pd
import structlog

from data.providers import clean_ohlcv

log = structlog.get_logger(__name__)

DateLike = Union[str, date, datetime, pd.Timestamp]

# Extra calendar days fetched before the backtest start so the longest
# indicator (EMA-200 ≈ 200 trading days) is warmed up on day one.
DEFAULT_WARMUP_DAYS = 400


def to_timestamp(value: DateLike) -> pd.Timestamp:
    """Coerce a date-like value to a tz-naive :class:`pandas.Timestamp`."""
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts


def load_price_history(
    symbols: List[str],
    start: DateLike,
    end: DateLike,
    warmup_days: int = DEFAULT_WARMUP_DAYS,
) -> Dict[str, pd.DataFrame]:
    """Fetch warmed-up daily OHLCV history for *symbols* from Yahoo Finance.

    Args:
        symbols: Ticker symbols to load.
        start: First trading day of the backtest window.
        end: Last trading day of the backtest window (inclusive).
        warmup_days: Calendar days of history to prepend before *start* so
            long-period indicators are valid from day one.

    Returns:
        Mapping of symbol to a canonical OHLCV DataFrame (DatetimeIndex,
        columns ``[Open, High, Low, Close, Volume]``).  Symbols that fail to
        load are omitted.
    """
    import yfinance as yf

    start_ts = to_timestamp(start)
    end_ts = to_timestamp(end)
    fetch_start = start_ts - timedelta(days=warmup_days)
    # yfinance treats ``end`` as exclusive; add a day to include the last bar.
    fetch_end = end_ts + timedelta(days=1)

    out: Dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        try:
            raw = yf.Ticker(symbol).history(
                start=fetch_start.date(),
                end=fetch_end.date(),
                auto_adjust=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("backtest.load_failed", symbol=symbol, error=str(exc))
            continue
        cleaned = clean_ohlcv(raw, symbol, log.bind(symbol=symbol))
        if cleaned is not None:
            # Normalise the index to tz-naive dates for stable comparisons.
            if isinstance(cleaned.index, pd.DatetimeIndex) and cleaned.index.tz:
                cleaned.index = cleaned.index.tz_localize(None)
            out[symbol] = cleaned

    log.info(
        "backtest.data_loaded",
        requested=len(symbols),
        loaded=len(out),
        start=str(start_ts.date()),
        end=str(end_ts.date()),
    )
    return out
