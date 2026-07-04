"""
Market-data provider abstraction for the US Trading Bot.

The rest of the codebase talks to market data through :mod:`data.fetcher`,
which delegates to whichever :class:`MarketDataProvider` is selected by
``settings.MARKET_DATA_PROVIDER``.  Two providers ship:

* :class:`YFinanceProvider` -- the default, free Yahoo Finance backend.  Daily
  bars only; no realtime streaming.
* :class:`AlpacaProvider` -- Alpaca market data (historical bars + latest
  trade) with optional websocket streaming for low-latency exits.  ``alpaca``
  (the ``alpaca-py`` package) is imported lazily so the default path never
  requires it.

Providers return OHLCV frames in the canonical shape the indicators expect: a
:class:`pandas.DatetimeIndex` and columns ``[Open, High, Low, Close, Volume]``.
On "no data" they return ``None``; on a transient backend error they *raise*
so the caller (:mod:`data.fetcher`) can retry with backoff.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, List, Optional, Protocol, runtime_checkable

import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

# Canonical OHLCV columns every provider must emit.
_EXPECTED_COLS: List[str] = ["Open", "High", "Low", "Close", "Volume"]

# Minimum rows for the longest indicator (EMA-200); below this we warn.
_MIN_ROWS_FOR_EMA200 = 200


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class MarketDataProvider(Protocol):
    """Interface every market-data backend must implement.

    Attributes:
        name: Short provider identifier (``"yfinance"``, ``"alpaca"``).
    """

    name: str

    def get_ohlcv(self, symbol: str, period: str = "6mo") -> Optional[pd.DataFrame]:
        """Return daily OHLCV bars for *symbol*, or ``None`` if unavailable."""
        ...

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Return the latest price for *symbol*, or ``None`` if unavailable."""
        ...

    def supports_streaming(self) -> bool:
        """Return whether the provider can stream realtime trades."""
        ...


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def clean_ohlcv(
    df: Optional[pd.DataFrame],
    symbol: str,
    log: "structlog.stdlib.BoundLogger",
) -> Optional[pd.DataFrame]:
    """Normalise a raw OHLCV frame to the canonical schema.

    Keeps only the standard OHLCV columns, drops NaN rows, ensures a
    :class:`~pandas.DatetimeIndex`, and warns when there are too few rows for
    long-period indicators.  Returns ``None`` when the frame is empty or
    missing required columns.
    """
    if df is None or df.empty:
        log.warning("provider.empty_data", symbol=symbol)
        return None

    missing = [c for c in _EXPECTED_COLS if c not in df.columns]
    if missing:
        log.warning("provider.missing_columns", symbol=symbol, missing=missing)
        return None

    df = df[_EXPECTED_COLS].copy()
    df.dropna(inplace=True)
    if df.empty:
        log.warning("provider.all_nan", symbol=symbol)
        return None

    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)

    if len(df) < _MIN_ROWS_FOR_EMA200:
        log.warning(
            "provider.insufficient_rows",
            symbol=symbol,
            rows=len(df),
            minimum=_MIN_ROWS_FOR_EMA200,
        )
    return df


def period_to_start(period: str, *, now: Optional[datetime] = None) -> datetime:
    """Convert a yfinance-style *period* string to an absolute start datetime.

    Understands ``Nd`` / ``Nmo`` / ``Ny`` (e.g. ``"5d"``, ``"6mo"``, ``"2y"``)
    and the special value ``"max"``.  Unknown values fall back to ~6 months.
    """
    now = now or datetime.now()
    p = period.strip().lower()
    if p == "max":
        return now - timedelta(days=3650)
    try:
        if p.endswith("mo"):
            return now - timedelta(days=int(p[:-2]) * 30)
        if p.endswith("d"):
            return now - timedelta(days=int(p[:-1]))
        if p.endswith("y"):
            return now - timedelta(days=int(p[:-1]) * 365)
        if p.endswith("wk"):
            return now - timedelta(weeks=int(p[:-2]))
    except ValueError:
        pass
    return now - timedelta(days=180)


# ---------------------------------------------------------------------------
# yfinance provider (default)
# ---------------------------------------------------------------------------


class YFinanceProvider:
    """Yahoo Finance provider backed by the ``yfinance`` library."""

    name = "yfinance"

    def __init__(self, settings: object | None = None) -> None:
        self._settings = settings
        self._log = logger.bind(provider="yfinance")

    def get_ohlcv(self, symbol: str, period: str = "6mo") -> Optional[pd.DataFrame]:
        import yfinance as yf

        log = self._log.bind(symbol=symbol, period=period)
        ticker = yf.Ticker(symbol)
        raw = ticker.history(period=period, auto_adjust=True)
        return clean_ohlcv(raw, symbol, log)

    def get_current_price(self, symbol: str) -> Optional[float]:
        import yfinance as yf

        log = self._log.bind(symbol=symbol)
        ticker = yf.Ticker(symbol)

        # fast_info is the quickest path -- avoids downloading full history.
        try:
            price = ticker.fast_info.get("lastPrice")
            if price is not None and price > 0:
                return float(price)
        except (AttributeError, KeyError, TypeError):
            pass

        # Fallback: most recent close from a short history window.
        df = ticker.history(period="5d", auto_adjust=True)
        if df is not None and not df.empty and "Close" in df.columns:
            closes = df["Close"].dropna()
            if not closes.empty:
                return float(closes.iloc[-1])

        log.warning("provider.price_unavailable", symbol=symbol)
        return None

    def supports_streaming(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Alpaca provider (optional -- requires alpaca-py + API keys)
# ---------------------------------------------------------------------------


class AlpacaProvider:
    """Alpaca market-data provider with optional websocket streaming.

    Historical bars and the latest trade come from Alpaca's REST data API;
    :meth:`stream_trades` opens a websocket for realtime trade updates.  The
    ``alpaca`` package is imported lazily inside each method so importing this
    module never requires it.
    """

    name = "alpaca"

    def __init__(self, settings: object) -> None:
        self._settings = settings
        self._key = getattr(settings, "ALPACA_API_KEY", "")
        self._secret = getattr(settings, "ALPACA_API_SECRET", "")
        self._feed = getattr(settings, "ALPACA_DATA_FEED", "iex")
        self._hist_client = None  # lazily constructed
        self._stream = None
        self._log = logger.bind(provider="alpaca", feed=self._feed)

    # -------------------------------------------------------------- clients

    def _client(self):
        """Return (and cache) an Alpaca historical-data client."""
        if self._hist_client is None:
            from alpaca.data.historical import StockHistoricalDataClient

            self._hist_client = StockHistoricalDataClient(self._key, self._secret)
        return self._hist_client

    def _feed_enum(self):
        from alpaca.data.enums import DataFeed

        return DataFeed.SIP if str(self._feed).lower() == "sip" else DataFeed.IEX

    # -------------------------------------------------------------- history

    def get_ohlcv(self, symbol: str, period: str = "6mo") -> Optional[pd.DataFrame]:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        log = self._log.bind(symbol=symbol, period=period)
        start = period_to_start(period)
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            feed=self._feed_enum(),
        )
        bars = self._client().get_stock_bars(request)
        raw = self._bars_to_frame(bars, symbol)
        return clean_ohlcv(raw, symbol, log)

    @staticmethod
    def _bars_to_frame(bars: object, symbol: str) -> Optional[pd.DataFrame]:
        """Convert an Alpaca ``BarSet`` into a canonical OHLCV DataFrame."""
        data = getattr(bars, "data", None)
        rows = data.get(symbol) if isinstance(data, dict) else None
        if not rows:
            return None
        frame = pd.DataFrame(
            [
                {
                    "Open": float(b.open),
                    "High": float(b.high),
                    "Low": float(b.low),
                    "Close": float(b.close),
                    "Volume": float(b.volume),
                    "_ts": b.timestamp,
                }
                for b in rows
            ]
        )
        frame.index = pd.to_datetime(frame.pop("_ts"))
        frame.index.name = "Date"
        return frame

    def get_current_price(self, symbol: str) -> Optional[float]:
        from alpaca.data.requests import StockLatestTradeRequest

        log = self._log.bind(symbol=symbol)
        request = StockLatestTradeRequest(
            symbol_or_symbols=symbol, feed=self._feed_enum()
        )
        latest = self._client().get_stock_latest_trade(request)
        trade = latest.get(symbol) if isinstance(latest, dict) else None
        if trade is None:
            log.warning("provider.price_unavailable", symbol=symbol)
            return None
        price = float(getattr(trade, "price", 0.0) or 0.0)
        return price if price > 0 else None

    def supports_streaming(self) -> bool:
        return True

    # -------------------------------------------------------------- stream

    def stream_trades(
        self,
        symbols: List[str],
        handler: Callable[[str, float], None],
    ) -> None:
        """Open a blocking websocket that invokes *handler(symbol, price)*.

        Intended to be run on its own thread/task; it blocks until the stream
        is stopped.  Requires ``alpaca-py`` and valid API keys.
        """
        from alpaca.data.live import StockDataStream

        self._stream = StockDataStream(self._key, self._secret)

        async def _on_trade(trade: object) -> None:
            sym = getattr(trade, "symbol", "")
            price = float(getattr(trade, "price", 0.0) or 0.0)
            if sym and price > 0:
                handler(sym, price)

        self._stream.subscribe_trades(_on_trade, *symbols)
        self._log.info("provider.stream_start", symbols=symbols)
        self._stream.run()

    def stop_stream(self) -> None:
        """Stop an active websocket stream, if any."""
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception as exc:  # noqa: BLE001
                self._log.warning("provider.stream_stop_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_provider(settings: object) -> MarketDataProvider:
    """Return the provider selected by ``settings.MARKET_DATA_PROVIDER``."""
    provider = str(getattr(settings, "MARKET_DATA_PROVIDER", "yfinance")).lower()
    if provider == "alpaca":
        return AlpacaProvider(settings)  # type: ignore[return-value]
    return YFinanceProvider(settings)  # type: ignore[return-value]
