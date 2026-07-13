"""Tests for multi-interval (timeframe) support in the data layer.

The bot has always fetched *daily* bars.  These tests lock in the newer
ability to request other grains (intraday ``"1h"``/``"15m"``/``"5m"`` and
coarser ``"1wk"``/``"1mo"``) end-to-end: the canonical-interval helpers, each
provider's native mapping, and the fetcher's per-interval caching.  Daily
remains the default, so every pre-existing caller is unaffected.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime

import pandas as pd
import pytest

from config.settings import Settings
from data import fetcher
from data.providers import (
    PolygonProvider,
    YFinanceProvider,
    _yf_interval,
    clamp_period_for_interval,
    interval_spec,
    is_intraday,
    normalize_interval,
)


# --------------------------------------------------------------- helpers


def _raw_frame(rows: int = 210) -> pd.DataFrame:
    idx = pd.bdate_range(end=datetime(2024, 1, 1), periods=rows)
    return pd.DataFrame(
        {
            "Open": range(1, rows + 1),
            "High": range(2, rows + 2),
            "Low": range(0, rows),
            "Close": range(1, rows + 1),
            "Volume": [1000] * rows,
        },
        index=idx,
    )


# --------------------------------------------------------- normalize_interval


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "1d"),
        ("1d", "1d"),
        ("daily", "1d"),
        ("60m", "1h"),
        ("hourly", "1h"),
        ("1w", "1wk"),
        ("WEEKLY", "1wk"),
        ("5min", "5m"),
    ],
)
def test_normalize_interval_aliases(raw, expected) -> None:
    assert normalize_interval(raw) == expected


def test_normalize_interval_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        normalize_interval("3s")


def test_interval_spec_and_is_intraday() -> None:
    assert interval_spec("15m") == (15, "minute")
    assert interval_spec("1h") == (1, "hour")
    assert interval_spec("1d") == (1, "day")
    assert is_intraday("5m") is True
    assert is_intraday("1h") is True
    assert is_intraday("1d") is False
    assert is_intraday("1wk") is False


# ------------------------------------------------------------- yfinance mapping


def test_yf_interval_mapping() -> None:
    assert _yf_interval("1d") == "1d"
    assert _yf_interval("1h") == "1h"
    assert _yf_interval("15m") == "15m"
    assert _yf_interval("1wk") == "1wk"
    assert _yf_interval("1mo") == "1mo"


def test_clamp_period_for_interval() -> None:
    # Daily/coarser grains are never clamped.
    assert clamp_period_for_interval("2y", "1d") == "2y"
    assert clamp_period_for_interval("2y", "1wk") == "2y"
    # Intraday grains are capped to what free tiers actually serve.
    assert clamp_period_for_interval("2y", "1m") == "7d"
    assert clamp_period_for_interval("2y", "15m") == "60d"
    assert clamp_period_for_interval("5y", "1h") == "730d"
    # A period already within the hourly cap is left alone.
    assert clamp_period_for_interval("1y", "1h") == "1y"
    # A period already within the cap is left alone.
    assert clamp_period_for_interval("5d", "15m") == "5d"


# ---------------------------------------------------- yfinance provider passthrough


class _FakeTicker:
    def __init__(self, df):
        self._df = df
        self.calls: list[dict] = []

    def history(self, period="6mo", interval="1d", auto_adjust=True, **kw):
        self.calls.append({"period": period, "interval": interval})
        return self._df


def test_yfinance_passes_interval(monkeypatch) -> None:
    ticker = _FakeTicker(_raw_frame())
    module = types.ModuleType("yfinance")
    module.Ticker = lambda symbol: ticker
    monkeypatch.setitem(sys.modules, "yfinance", module)

    YFinanceProvider().get_ohlcv("AAPL", period="2y", interval="15m")
    # Interval reaches yfinance and the intraday period is clamped.
    assert ticker.calls[0]["interval"] == "15m"
    assert ticker.calls[0]["period"] == "60d"


# ----------------------------------------------------- polygon URL construction


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_polygon_builds_intraday_url(monkeypatch) -> None:
    provider = PolygonProvider(Settings(POLYGON_API_KEY="k"))
    captured: dict = {}

    def fake_get(url, params=None, timeout=None, headers=None):
        captured["url"] = url
        results = [
            {"o": 1, "h": 2, "l": 0, "c": 1, "v": 1000,
             "t": 1_700_000_000_000 + i * 3_600_000}
            for i in range(210)
        ]
        return _FakeResponse({"results": results})

    monkeypatch.setattr("httpx.get", fake_get)
    provider.get_ohlcv("AAPL", period="1y", interval="1h")
    assert "/range/1/hour/" in captured["url"]

    provider.get_ohlcv("AAPL", period="1y", interval="15m")
    assert "/range/15/minute/" in captured["url"]

    provider.get_ohlcv("AAPL", period="1y", interval="1d")
    assert "/range/1/day/" in captured["url"]


# ------------------------------------------------------------- fetcher caching


class _CountingProvider:
    name = "counting"

    def __init__(self, df):
        self._df = df
        self.calls: list[tuple] = []

    def get_ohlcv(self, symbol, period="6mo", interval="1d"):
        self.calls.append((symbol, period, interval))
        return self._df

    def get_current_price(self, symbol):
        return 1.0

    def supports_streaming(self):
        return False


def test_fetch_ohlcv_caches_per_interval(monkeypatch) -> None:
    provider = _CountingProvider(_raw_frame())
    fetcher.set_provider(provider)
    fetcher.clear_cache()
    try:
        fetcher.fetch_ohlcv("AAPL", period="1y", interval="1d")
        fetcher.fetch_ohlcv("AAPL", period="1y", interval="1d")  # cache hit
        assert len(provider.calls) == 1

        # A different interval is a distinct cache entry -> new backend call.
        fetcher.fetch_ohlcv("AAPL", period="1y", interval="1h")
        assert len(provider.calls) == 2
        assert provider.calls[-1] == ("AAPL", "1y", "1h")
    finally:
        fetcher.set_provider(None)
        fetcher.clear_cache()
