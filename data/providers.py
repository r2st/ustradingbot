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

import time
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Protocol, runtime_checkable

import pandas as pd
import structlog

from config.settings import EASTERN

logger = structlog.get_logger(__name__)

# Canonical OHLCV columns every provider must emit.
_EXPECTED_COLS: List[str] = ["Open", "High", "Low", "Close", "Volume"]

# Minimum rows for the longest indicator (EMA-200); below this we warn.
_MIN_ROWS_FOR_EMA200 = 200


def _extended_hours_session(now: datetime) -> str:
    """Classify an Eastern-time datetime into a market session tag.

    Returns ``"pre"`` (04:00–09:30), ``"regular"`` (09:30–16:00),
    ``"post"`` (16:00–20:00), or ``"closed"`` otherwise.  Used to label
    extended-hours quotes.
    """
    minutes = now.hour * 60 + now.minute
    if 4 * 60 <= minutes < 9 * 60 + 30:
        return "pre"
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return "regular"
    if 16 * 60 <= minutes < 20 * 60:
        return "post"
    return "closed"


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

    # Optional (Feature 4): extended-hours quote.  Providers that cannot serve
    # pre/post-market prints simply omit this method — callers duck-type it via
    # ``getattr(provider, "get_extended_hours_quote", None)`` and fail-open to
    # ``None``.  Declared here for documentation; not required by the Protocol.


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def clean_ohlcv(
    df: Optional[pd.DataFrame],
    symbol: str,
    log: "structlog.stdlib.BoundLogger",
    period: Optional[str] = None,
) -> Optional[pd.DataFrame]:
    """Normalise a raw OHLCV frame to the canonical schema.

    Keeps only the standard OHLCV columns, drops NaN rows, ensures a
    :class:`~pandas.DatetimeIndex`, and warns when there are too few rows for
    long-period indicators.  Returns ``None`` when the frame is empty or
    missing required columns.

    When *period* is given, the too-few-rows warning only fires if the
    requested window could actually contain ``_MIN_ROWS_FOR_EMA200`` trading
    days -- a 6-month request can never yield 200 daily bars, so warning about
    it is pure noise.
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

    if len(df) < _MIN_ROWS_FOR_EMA200 and _period_can_hold(period, _MIN_ROWS_FOR_EMA200):
        log.warning(
            "provider.insufficient_rows",
            symbol=symbol,
            rows=len(df),
            minimum=_MIN_ROWS_FOR_EMA200,
        )
    return df


def _period_can_hold(period: Optional[str], rows: int) -> bool:
    """Return whether *period* could plausibly contain *rows* trading days.

    Unknown/absent periods return ``True`` so the caller keeps warning (the
    conservative pre-existing behaviour).  ~252 trading days per 365 calendar
    days.
    """
    if not period:
        return True
    calendar_days = (datetime.now(tz=EASTERN) - period_to_start(period)).days
    return calendar_days * 252 / 365 >= rows


def period_to_start(period: str, *, now: Optional[datetime] = None) -> datetime:
    """Convert a yfinance-style *period* string to an absolute start datetime.

    Understands ``Nd`` / ``Nmo`` / ``Ny`` (e.g. ``"5d"``, ``"6mo"``, ``"2y"``)
    and the special value ``"max"``.  Unknown values fall back to ~6 months.
    """
    now = now or datetime.now(tz=EASTERN)
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
        return clean_ohlcv(raw, symbol, log, period=period)

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
        return clean_ohlcv(raw, symbol, log, period=period)

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

    # ------------------------------------------------------ extended hours

    def get_extended_hours_quote(self, symbol: str) -> Optional[dict]:
        """Return a pre/post-market quote for *symbol* (Feature 4), or ``None``.

        Uses Alpaca's latest trade (which includes extended-hours prints on the
        IEX feed) plus today's prior daily close for the overnight gap.  Returns
        a plain dict (``last``, ``prev_close``, ``session``, ``ext_volume``,
        ``avg_ext_volume``) that :mod:`data.extended_hours` wraps in an
        ``ExtQuote``.  Best-effort: any error returns ``None``.
        """
        log = self._log.bind(symbol=symbol)
        try:
            last = self.get_current_price(symbol)
            if not last:
                return None
            # Prior daily close from a short history window.
            hist = self.get_ohlcv(symbol, period="5d")
            prev_close = (
                float(hist["Close"].iloc[-1]) if hist is not None and len(hist) else None
            )
            session = _extended_hours_session(datetime.now(tz=EASTERN))
            return {
                "last": float(last),
                "prev_close": prev_close,
                "session": session,
                "ext_volume": None,
                "avg_ext_volume": None,
            }
        except Exception as exc:  # noqa: BLE001 -- fail-open
            log.warning("provider.ext_hours_failed", symbol=symbol, error=str(exc))
            return None

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
# Polygon.io provider (optional -- requires POLYGON_API_KEY)
# ---------------------------------------------------------------------------


class PolygonProvider:
    """Polygon.io market-data provider backed by the REST API.

    Uses ``httpx`` directly (already a project dependency) so no extra package
    is required.  Daily aggregate bars come from the ``/v2/aggs`` endpoint and
    the latest price from ``/v2/last/trade``.  Polygon covers US equities only.
    """

    name = "polygon"
    _BASE = "https://api.polygon.io"
    # Non-US exchange suffixes Polygon can never serve (TSX, TSX-V, CSE, NEO).
    # Requesting them just burns the free tier's ~5 req/min budget on
    # guaranteed-empty responses, starving the US symbols into HTTP 429.
    _UNSUPPORTED_SUFFIXES = (".TO", ".V", ".CN", ".NE")

    def __init__(self, settings: object) -> None:
        self._settings = settings
        self._key = getattr(settings, "POLYGON_API_KEY", "")
        self._log = logger.bind(provider="polygon")

    def supports_symbol(self, symbol: str) -> bool:
        """Return whether Polygon covers *symbol* (US listings only)."""
        return not symbol.upper().endswith(self._UNSUPPORTED_SUFFIXES)

    def _auth_headers(self) -> dict:
        # Bearer header instead of an ``apiKey`` query param so the key never
        # appears in URLs echoed back by httpx error messages / logs.
        return {"Authorization": f"Bearer {self._key}"}

    def get_ohlcv(self, symbol: str, period: str = "6mo") -> Optional[pd.DataFrame]:
        import httpx

        log = self._log.bind(symbol=symbol, period=period)
        if not self.supports_symbol(symbol):
            log.debug("provider.symbol_unsupported", symbol=symbol)
            return None
        start = period_to_start(period).strftime("%Y-%m-%d")
        end = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
        url = (
            f"{self._BASE}/v2/aggs/ticker/{symbol}/range/1/day/{start}/{end}"
        )
        resp = httpx.get(
            url,
            params={"adjusted": "true", "sort": "asc", "limit": 50000},
            headers=self._auth_headers(),
            timeout=30.0,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        raw = self._aggs_to_frame(results)
        return clean_ohlcv(raw, symbol, log, period=period)

    @staticmethod
    def _aggs_to_frame(results: list) -> Optional[pd.DataFrame]:
        """Convert Polygon aggregate bars to a canonical OHLCV DataFrame."""
        if not results:
            return None
        frame = pd.DataFrame(
            [
                {
                    "Open": float(b.get("o", 0.0)),
                    "High": float(b.get("h", 0.0)),
                    "Low": float(b.get("l", 0.0)),
                    "Close": float(b.get("c", 0.0)),
                    "Volume": float(b.get("v", 0.0)),
                    "_ts": b.get("t", 0),
                }
                for b in results
            ]
        )
        # Polygon timestamps are epoch milliseconds.
        frame.index = pd.to_datetime(frame.pop("_ts"), unit="ms")
        frame.index.name = "Date"
        return frame

    def get_current_price(self, symbol: str) -> Optional[float]:
        import httpx

        log = self._log.bind(symbol=symbol)
        if not self.supports_symbol(symbol):
            log.debug("provider.symbol_unsupported", symbol=symbol)
            return None
        url = f"{self._BASE}/v2/last/trade/{symbol}"
        try:
            resp = httpx.get(url, headers=self._auth_headers(), timeout=15.0)
            resp.raise_for_status()
            body = resp.json()
            # Newer schema: {"results": {"p": price}}; older: {"last": {"price": ...}}
            results = body.get("results") or {}
            price = results.get("p")
            if price is None:
                price = (body.get("last") or {}).get("price")
            if price is not None and float(price) > 0:
                return float(price)
            log.warning("provider.price_unavailable", symbol=symbol)
        except httpx.HTTPStatusError as exc:
            # The real-time last-trade endpoint requires a paid Polygon plan and
            # returns 403 on the free tier.  Fall back to the most recent daily
            # close (served by the free aggregates plan) so daily-bar strategies
            # still get a usable price instead of failing every freshness check.
            if exc.response.status_code != 403:
                log.warning("provider.last_trade_failed",
                            status=exc.response.status_code)
        except Exception:  # noqa: BLE001
            log.warning("provider.last_trade_error")

        return self._previous_close(symbol, log)

    def _previous_close(self, symbol: str, log) -> Optional[float]:
        """Return the prior session's close as a delayed price fallback."""
        import httpx

        url = f"{self._BASE}/v2/aggs/ticker/{symbol}/prev"
        try:
            resp = httpx.get(
                url, params={"adjusted": "true"},
                headers=self._auth_headers(),
                timeout=15.0,
            )
            resp.raise_for_status()
            results = resp.json().get("results") or []
            close = results[0].get("c") if results else None
            if close is not None and float(close) > 0:
                log.info("provider.price_from_prev_close", price=float(close))
                return float(close)
        except Exception:  # noqa: BLE001
            log.warning("provider.prev_close_failed", symbol=symbol)
        log.warning("provider.price_unavailable", symbol=symbol)
        return None

    def supports_streaming(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Fallback provider (primary -> secondary chaining with a circuit breaker)
# ---------------------------------------------------------------------------


class FallbackProvider:
    """Chain two providers: use *primary*, fall back to *fallback* on failure.

    A provider "fails" for a call when it raises an exception *or* returns
    ``None``.  This exists because free market-data tiers are unreliable at
    scan volume -- most importantly Polygon's free plan, which caps at ~5
    requests/minute and returns HTTP 429 for the rest of a 41-symbol scan.
    Without a fallback the screener receives no data and generates zero
    signals, so the engine places no trades at all.

    A lightweight circuit breaker prevents pointlessly hammering a
    rate-limited primary once per symbol: after ``trip_threshold`` consecutive
    primary failures the breaker "trips" and every call goes straight to the
    fallback for ``cooldown_seconds``.  Any later primary success resets it.
    """

    def __init__(
        self,
        primary: MarketDataProvider,
        fallback: MarketDataProvider,
        *,
        trip_threshold: int = 3,
        cooldown_seconds: float = 300.0,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._trip_threshold = max(1, int(trip_threshold))
        self._cooldown = max(0.0, float(cooldown_seconds))
        self._consecutive_failures = 0
        self._tripped_until = 0.0
        self.name = f"{primary.name}->{fallback.name}"
        self._log = logger.bind(provider=self.name)

    # ---------------------------------------------------------- breaker state

    def _breaker_open(self) -> bool:
        """Return whether the primary is currently being skipped."""
        return time.monotonic() < self._tripped_until

    def _record_primary_success(self) -> None:
        self._consecutive_failures = 0
        self._tripped_until = 0.0

    def _record_primary_failure(self, *, rate_limited: bool = False) -> None:
        self._consecutive_failures += 1
        # A rate-limit response means every further call this window will also
        # fail, so don't wait for the threshold -- trip immediately.
        if rate_limited:
            self._consecutive_failures = max(
                self._consecutive_failures, self._trip_threshold
            )
        if self._consecutive_failures >= self._trip_threshold and not self._breaker_open():
            self._tripped_until = time.monotonic() + self._cooldown
            self._log.warning(
                "provider.fallback_breaker_tripped",
                primary=self._primary.name,
                fallback=self._fallback.name,
                consecutive_failures=self._consecutive_failures,
                cooldown_seconds=self._cooldown,
            )

    # ---------------------------------------------------------------- dispatch

    def _call(self, method: str, symbol: str, *args) -> Optional[object]:
        """Invoke *method* on the primary, falling back on failure.

        Only the fallback is allowed to raise (so :func:`data.fetcher._with_retry`
        can still retry a genuinely-down fallback); primary errors are swallowed
        and turned into a fallback attempt.
        """
        supports = getattr(self._primary, "supports_symbol", None)
        primary_eligible = supports(symbol) if callable(supports) else True

        if primary_eligible and not self._breaker_open():
            try:
                result = getattr(self._primary, method)(symbol, *args)
                if result is not None:
                    self._record_primary_success()
                    return result
                # Empty result counts as a soft failure -> try the fallback.
                self._record_primary_failure()
            except Exception as exc:  # noqa: BLE001 -- classified as transient
                status = getattr(getattr(exc, "response", None), "status_code", None)
                self._record_primary_failure(rate_limited=status == 429)
                self._log.warning(
                    "provider.fallback_primary_error",
                    method=method,
                    symbol=symbol,
                    primary=self._primary.name,
                    error=str(exc),
                )

        result = getattr(self._fallback, method)(symbol, *args)
        if result is not None:
            self._log.info(
                "provider.fallback_used",
                method=method,
                symbol=symbol,
                fallback=self._fallback.name,
                breaker_open=self._breaker_open(),
            )
        return result

    def get_ohlcv(self, symbol: str, period: str = "6mo") -> Optional[pd.DataFrame]:
        return self._call("get_ohlcv", symbol, period)  # type: ignore[return-value]

    def get_current_price(self, symbol: str) -> Optional[float]:
        return self._call("get_current_price", symbol)  # type: ignore[return-value]

    def supports_streaming(self) -> bool:
        # Streaming exits key off the primary's capabilities; the fallback is
        # only used for one-shot REST fetches.
        return self._primary.supports_streaming()

    def get_extended_hours_quote(self, symbol: str) -> Optional[dict]:
        """Duck-typed passthrough (Feature 4): try primary then fallback.

        A provider without the method contributes ``None``; the whole thing
        fails open so extended-hours support is best-effort per provider.
        """
        for provider in (self._primary, self._fallback):
            method = getattr(provider, "get_extended_hours_quote", None)
            if not callable(method):
                continue
            try:
                result = method(symbol)
            except Exception:  # noqa: BLE001 -- fail-open
                result = None
            if result is not None:
                return result
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _make_single_provider(name: str, settings: object) -> MarketDataProvider:
    """Construct one concrete provider by short name."""
    if name == "alpaca":
        return AlpacaProvider(settings)  # type: ignore[return-value]
    if name == "polygon":
        return PolygonProvider(settings)  # type: ignore[return-value]
    return YFinanceProvider(settings)  # type: ignore[return-value]


def make_provider(settings: object) -> MarketDataProvider:
    """Return the provider selected by ``settings.MARKET_DATA_PROVIDER``.

    When ``MARKET_DATA_FALLBACK_PROVIDER`` names a *different* provider, the
    primary is wrapped in a :class:`FallbackProvider` so rate-limit/outage
    failures on the primary transparently fall back to the secondary backend.
    """
    primary_name = str(getattr(settings, "MARKET_DATA_PROVIDER", "yfinance")).lower()
    primary = _make_single_provider(primary_name, settings)

    fallback_name = str(
        getattr(settings, "MARKET_DATA_FALLBACK_PROVIDER", "") or ""
    ).lower()
    if not fallback_name or fallback_name == primary_name:
        return primary

    fallback = _make_single_provider(fallback_name, settings)
    logger.info(
        "provider.fallback_enabled",
        primary=primary_name,
        fallback=fallback_name,
    )
    return FallbackProvider(  # type: ignore[return-value]
        primary,
        fallback,
        trip_threshold=int(getattr(settings, "PROVIDER_FALLBACK_TRIP_THRESHOLD", 3)),
        cooldown_seconds=float(
            getattr(settings, "PROVIDER_FALLBACK_COOLDOWN_SECONDS", 300.0)
        ),
    )
