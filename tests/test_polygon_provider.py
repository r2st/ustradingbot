"""Tests for the Polygon.io market-data provider."""

from __future__ import annotations

import pandas as pd
import pytest

from config.settings import Settings
from data.providers import PolygonProvider, make_provider


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_make_provider_selects_polygon() -> None:
    s = Settings(MARKET_DATA_PROVIDER="polygon", POLYGON_API_KEY="k")
    assert isinstance(make_provider(s), PolygonProvider)


def test_aggs_to_frame() -> None:
    results = [
        {"o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000, "t": 1_700_000_000_000},
        {"o": 10.5, "h": 12, "l": 10, "c": 11.5, "v": 1500, "t": 1_700_086_400_000},
    ]
    frame = PolygonProvider._aggs_to_frame(results)
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert frame.iloc[0]["Close"] == 10.5


def test_aggs_to_frame_empty() -> None:
    assert PolygonProvider._aggs_to_frame([]) is None


def test_get_ohlcv_calls_polygon(monkeypatch) -> None:
    s = Settings(MARKET_DATA_PROVIDER="polygon", POLYGON_API_KEY="secret-key")
    provider = PolygonProvider(s)

    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        # 210 rows so clean_ohlcv doesn't warn about insufficient data
        results = [
            {"o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100 + i,
             "v": 1000, "t": 1_700_000_000_000 + i * 86_400_000}
            for i in range(210)
        ]
        return FakeResponse({"results": results})

    monkeypatch.setattr("httpx.get", fake_get)
    df = provider.get_ohlcv("AAPL", period="1y")
    assert df is not None and len(df) == 210
    assert "AAPL" in captured["url"]
    assert captured["params"]["apiKey"] == "secret-key"


def test_get_current_price_new_schema(monkeypatch) -> None:
    provider = PolygonProvider(Settings(POLYGON_API_KEY="k"))
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResponse({"results": {"p": 142.5}}))
    assert provider.get_current_price("AAPL") == 142.5


def test_get_current_price_old_schema(monkeypatch) -> None:
    provider = PolygonProvider(Settings(POLYGON_API_KEY="k"))
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResponse({"last": {"price": 99.0}}))
    assert provider.get_current_price("AAPL") == 99.0


def test_get_current_price_missing(monkeypatch) -> None:
    provider = PolygonProvider(Settings(POLYGON_API_KEY="k"))
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResponse({"results": {}}))
    assert provider.get_current_price("AAPL") is None


def test_polygon_no_streaming() -> None:
    assert PolygonProvider(Settings()).supports_streaming() is False
