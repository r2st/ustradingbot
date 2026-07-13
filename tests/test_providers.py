"""Tests for the market-data provider abstraction (yfinance + alpaca)."""

from __future__ import annotations

import sys
import types
from datetime import datetime

import pandas as pd
import pytest

from config.settings import Settings
from data.providers import (
    AlpacaProvider,
    FallbackProvider,
    PolygonProvider,
    YFinanceProvider,
    clean_ohlcv,
    make_provider,
    period_to_start,
)


# ------------------------------------------------------------------ helpers


def _raw_frame(n=210) -> pd.DataFrame:
    idx = pd.bdate_range(end=datetime(2024, 1, 1), periods=n)
    base = pd.Series(range(1, n + 1), index=idx, dtype=float)
    return pd.DataFrame(
        {
            "Open": base,
            "High": base + 1,
            "Low": base - 1,
            "Close": base,
            "Volume": base * 1000,
            "Dividends": 0.0,  # extra columns should be dropped
            "Stock Splits": 0.0,
        }
    )


# ------------------------------------------------------------------ clean_ohlcv


def test_clean_ohlcv_keeps_canonical_columns() -> None:
    import structlog

    log = structlog.get_logger("t")
    out = clean_ohlcv(_raw_frame(), "AAPL", log)
    assert list(out.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert isinstance(out.index, pd.DatetimeIndex)


def test_clean_ohlcv_empty_returns_none() -> None:
    import structlog

    log = structlog.get_logger("t")
    assert clean_ohlcv(pd.DataFrame(), "AAPL", log) is None
    assert clean_ohlcv(None, "AAPL", log) is None


def test_clean_ohlcv_missing_columns_returns_none() -> None:
    import structlog

    log = structlog.get_logger("t")
    bad = pd.DataFrame({"Open": [1.0], "Close": [1.0]})
    assert clean_ohlcv(bad, "AAPL", log) is None


def test_clean_ohlcv_insufficient_rows_warning_is_period_aware() -> None:
    """Short windows can never hold 200 bars, so no warning for them."""
    from data.providers import _period_can_hold

    # A 6-month request tops out around ~126 trading days -- warning is noise.
    assert _period_can_hold("6mo", 200) is False
    assert _period_can_hold("5d", 200) is False
    assert _period_can_hold("3mo", 200) is False
    # A 1y/2y request genuinely should contain 200+ bars -- keep warning.
    assert _period_can_hold("1y", 200) is True
    assert _period_can_hold("2y", 200) is True
    # Unknown period keeps the conservative (warn) behaviour.
    assert _period_can_hold(None, 200) is True


# ------------------------------------------------------------------ period_to_start


@pytest.mark.parametrize(
    "period,days",
    [("5d", 5), ("6mo", 180), ("2y", 730), ("3mo", 90), ("1y", 365)],
)
def test_period_to_start(period, days) -> None:
    now = datetime(2024, 6, 1)
    start = period_to_start(period, now=now)
    assert (now - start).days == days


def test_period_to_start_max_and_unknown() -> None:
    now = datetime(2024, 6, 1)
    assert (now - period_to_start("max", now=now)).days == 3650
    assert (now - period_to_start("garbage", now=now)).days == 180


# ------------------------------------------------------------------ factory


def test_make_provider_default_is_yfinance() -> None:
    # Default primary and fallback are both yfinance, so the factory returns the
    # bare provider (no self-wrapping when primary == fallback).
    assert isinstance(make_provider(Settings()), YFinanceProvider)


def test_make_provider_alpaca() -> None:
    # Disable the fallback to assert the raw primary selection.
    assert isinstance(
        make_provider(
            Settings(MARKET_DATA_PROVIDER="alpaca", MARKET_DATA_FALLBACK_PROVIDER="")
        ),
        AlpacaProvider,
    )


def test_make_provider_wraps_primary_with_fallback() -> None:
    # A non-yfinance primary with the default yfinance fallback is wrapped so a
    # rate-limited/erroring primary transparently falls back to Yahoo.
    from data.providers import FallbackProvider

    provider = make_provider(Settings(MARKET_DATA_PROVIDER="polygon"))
    assert isinstance(provider, FallbackProvider)
    assert isinstance(provider._primary, PolygonProvider)
    assert isinstance(provider._fallback, YFinanceProvider)


def test_make_provider_no_fallback_when_disabled() -> None:
    provider = make_provider(
        Settings(MARKET_DATA_PROVIDER="polygon", MARKET_DATA_FALLBACK_PROVIDER="")
    )
    assert isinstance(provider, PolygonProvider)


def test_fallback_provider_uses_fallback_on_primary_error() -> None:
    """Primary raising -> fallback result is returned and no error propagates."""
    from data.providers import FallbackProvider

    class _Boom:
        name = "boom"

        def get_ohlcv(self, symbol, period="6mo", interval="1d"):
            raise RuntimeError("429 Too Many Requests")

        def get_current_price(self, symbol):
            raise RuntimeError("429")

        def supports_streaming(self):
            return False

    class _Good:
        name = "good"

        def get_ohlcv(self, symbol, period="6mo", interval="1d"):
            return "DATA"

        def get_current_price(self, symbol):
            return 123.0

        def supports_streaming(self):
            return False

    fb = FallbackProvider(_Boom(), _Good(), trip_threshold=3, cooldown_seconds=300)
    assert fb.get_ohlcv("AAPL") == "DATA"
    assert fb.get_current_price("AAPL") == 123.0


def test_fallback_provider_breaker_trips_after_threshold() -> None:
    """After N consecutive primary failures, the primary is skipped entirely."""
    from data.providers import FallbackProvider

    class _Counter:
        name = "counter"
        calls = 0

        def get_ohlcv(self, symbol, period="6mo", interval="1d"):
            _Counter.calls += 1
            raise RuntimeError("429")

        def get_current_price(self, symbol):
            return None

        def supports_streaming(self):
            return False

    class _Good:
        name = "good"

        def get_ohlcv(self, symbol, period="6mo", interval="1d"):
            return "DATA"

        def get_current_price(self, symbol):
            return 1.0

        def supports_streaming(self):
            return False

    primary = _Counter()
    fb = FallbackProvider(primary, _Good(), trip_threshold=3, cooldown_seconds=300)
    for _ in range(20):
        assert fb.get_ohlcv("X") == "DATA"
    # Primary is only tried up to the trip threshold, then the breaker opens.
    assert _Counter.calls == 3
    assert fb._breaker_open() is True


def test_fallback_provider_breaker_trips_immediately_on_429() -> None:
    """A rate-limit response opens the breaker on the first failure."""
    from data.providers import FallbackProvider

    class _Resp:
        status_code = 429

    class _RateLimited(Exception):
        response = _Resp()

    class _Limited:
        name = "limited"
        calls = 0

        def get_ohlcv(self, symbol, period="6mo", interval="1d"):
            _Limited.calls += 1
            raise _RateLimited("429 Too Many Requests")

        def get_current_price(self, symbol):
            return None

        def supports_streaming(self):
            return False

    class _Good:
        name = "good"

        def get_ohlcv(self, symbol, period="6mo", interval="1d"):
            return "DATA"

        def get_current_price(self, symbol):
            return 1.0

        def supports_streaming(self):
            return False

    fb = FallbackProvider(_Limited(), _Good(), trip_threshold=3, cooldown_seconds=300)
    for _ in range(10):
        assert fb.get_ohlcv("X") == "DATA"
    # One 429 is enough -- no point burning two more requests on a dead window.
    assert _Limited.calls == 1
    assert fb._breaker_open() is True


def test_fallback_provider_skips_primary_for_unsupported_symbols() -> None:
    """Unsupported symbols go straight to the fallback without breaker impact."""
    from data.providers import FallbackProvider

    class _USOnly:
        name = "us-only"
        calls = 0

        def supports_symbol(self, symbol):
            return not symbol.endswith(".TO")

        def get_ohlcv(self, symbol, period="6mo", interval="1d"):
            _USOnly.calls += 1
            return "US_DATA"

        def get_current_price(self, symbol):
            return 1.0

        def supports_streaming(self):
            return False

    class _Good:
        name = "good"

        def get_ohlcv(self, symbol, period="6mo", interval="1d"):
            return "CA_DATA"

        def get_current_price(self, symbol):
            return 2.0

        def supports_streaming(self):
            return False

    fb = FallbackProvider(_USOnly(), _Good(), trip_threshold=3, cooldown_seconds=300)
    for _ in range(10):
        assert fb.get_ohlcv("SHOP.TO") == "CA_DATA"
    # The primary was never touched and the breaker never accumulated failures.
    assert _USOnly.calls == 0
    assert fb._breaker_open() is False
    assert fb._consecutive_failures == 0
    # US symbols still hit the primary.
    assert fb.get_ohlcv("AAPL") == "US_DATA"
    assert _USOnly.calls == 1


def test_yfinance_supports_streaming_false() -> None:
    assert YFinanceProvider().supports_streaming() is False


def test_alpaca_supports_streaming_true() -> None:
    assert AlpacaProvider(Settings()).supports_streaming() is True


# ------------------------------------------------------------------ yfinance


class _FakeTicker:
    def __init__(self, df, fast_price=None):
        self._df = df
        self.fast_info = {"lastPrice": fast_price} if fast_price is not None else {}

    def history(self, period="6mo", auto_adjust=True, **kw):
        return self._df


def _install_fake_yfinance(monkeypatch, ticker) -> None:
    module = types.ModuleType("yfinance")
    module.Ticker = lambda symbol: ticker
    monkeypatch.setitem(sys.modules, "yfinance", module)


def test_yfinance_get_ohlcv(monkeypatch) -> None:
    _install_fake_yfinance(monkeypatch, _FakeTicker(_raw_frame()))
    out = YFinanceProvider().get_ohlcv("AAPL")
    assert out is not None and len(out) == 210


def test_yfinance_price_fast_info(monkeypatch) -> None:
    _install_fake_yfinance(monkeypatch, _FakeTicker(_raw_frame(), fast_price=321.0))
    assert YFinanceProvider().get_current_price("AAPL") == 321.0


def test_yfinance_price_history_fallback(monkeypatch) -> None:
    # No fast price -> falls back to last close (which is n == 210).
    _install_fake_yfinance(monkeypatch, _FakeTicker(_raw_frame(), fast_price=None))
    assert YFinanceProvider().get_current_price("AAPL") == 210.0


# ------------------------------------------------------------------ alpaca (mocked)


class _FakeBar:
    def __init__(self, ts, o, h, l, c, v):
        self.timestamp = ts
        self.open, self.high, self.low, self.close, self.volume = o, h, l, c, v


def test_alpaca_bars_to_frame() -> None:
    ts = pd.Timestamp("2024-01-02")
    bars = types.SimpleNamespace(
        data={"AAPL": [_FakeBar(ts, 10, 11, 9, 10.5, 1000)]}
    )
    frame = AlpacaProvider._bars_to_frame(bars, "AAPL")
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert float(frame.iloc[0]["Close"]) == 10.5


def test_alpaca_bars_to_frame_missing_symbol() -> None:
    bars = types.SimpleNamespace(data={})
    assert AlpacaProvider._bars_to_frame(bars, "AAPL") is None


def _install_fake_alpaca(monkeypatch, *, bars=None, trade_price=None) -> None:
    """Install a minimal fake ``alpaca`` package tree in sys.modules."""
    root = types.ModuleType("alpaca")
    data = types.ModuleType("alpaca.data")
    historical = types.ModuleType("alpaca.data.historical")
    requests = types.ModuleType("alpaca.data.requests")
    timeframe = types.ModuleType("alpaca.data.timeframe")
    enums = types.ModuleType("alpaca.data.enums")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def get_stock_bars(self, request):
            return bars

        def get_stock_latest_trade(self, request):
            return {"AAPL": types.SimpleNamespace(price=trade_price)}

    historical.StockHistoricalDataClient = _Client
    requests.StockBarsRequest = lambda **kw: kw
    requests.StockLatestTradeRequest = lambda **kw: kw

    class _TimeFrame:
        Day = "1Day"

        def __init__(self, amount, unit):
            self.amount, self.unit = amount, unit

    timeframe.TimeFrame = _TimeFrame
    timeframe.TimeFrameUnit = types.SimpleNamespace(
        Minute="Min", Hour="Hour", Day="Day", Week="Week", Month="Month"
    )
    enums.DataFeed = types.SimpleNamespace(IEX="iex", SIP="sip")

    for name, mod in {
        "alpaca": root,
        "alpaca.data": data,
        "alpaca.data.historical": historical,
        "alpaca.data.requests": requests,
        "alpaca.data.timeframe": timeframe,
        "alpaca.data.enums": enums,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)


def test_alpaca_get_ohlcv_mocked(monkeypatch) -> None:
    ts_index = pd.bdate_range(end=datetime(2024, 1, 1), periods=210)
    bars = types.SimpleNamespace(
        data={"AAPL": [_FakeBar(t, 10, 11, 9, 10.5, 1000) for t in ts_index]}
    )
    _install_fake_alpaca(monkeypatch, bars=bars)
    out = AlpacaProvider(Settings(ALPACA_API_KEY="k", ALPACA_API_SECRET="s")).get_ohlcv("AAPL")
    assert out is not None and len(out) == 210


def test_alpaca_get_current_price_mocked(monkeypatch) -> None:
    _install_fake_alpaca(monkeypatch, trade_price=456.7)
    price = AlpacaProvider(Settings(ALPACA_API_KEY="k", ALPACA_API_SECRET="s")).get_current_price("AAPL")
    assert price == pytest.approx(456.7)
