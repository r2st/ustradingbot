"""Tests for the fetcher's TTL cache, retry/backoff, and provider wiring."""

from __future__ import annotations

import threading

import pandas as pd
import pytest

import data.fetcher as fetcher
from config.settings import Settings


class FakeProvider:
    """Counting provider whose behaviour is scripted per test."""

    name = "fake"

    def __init__(self, price=100.0, df=None, fail_first=0, raise_first=0):
        self.price = price
        self.df = df if df is not None else pd.DataFrame({"Close": [1.0]})
        self.fail_first = fail_first  # return None this many times
        self.raise_first = raise_first  # raise this many times
        self.ohlcv_calls = 0
        self.price_calls = 0

    def _maybe_fail(self, n):
        if n <= self.raise_first:
            raise RuntimeError("transient")
        if n <= self.raise_first + self.fail_first:
            return True
        return False

    def get_ohlcv(self, symbol, period="6mo", interval="1d"):
        self.ohlcv_calls += 1
        if self._maybe_fail(self.ohlcv_calls):
            return None
        return self.df

    def get_current_price(self, symbol):
        self.price_calls += 1
        if self._maybe_fail(self.price_calls):
            return None
        return self.price

    def supports_streaming(self):
        return False


@pytest.fixture
def fast_settings(monkeypatch):
    """Install controllable settings + a clean cache; no real sleeps."""
    monkeypatch.setattr(fetcher.time, "sleep", lambda *_: None)
    fetcher.clear_cache()
    fetcher.set_provider(None)

    def _install(**kw):
        base = dict(FETCH_RETRY_BASE_DELAY_SECONDS=0.0, FETCH_RETRY_MAX_DELAY_SECONDS=0.0)
        base.update(kw)
        settings = Settings(**base)
        monkeypatch.setattr(fetcher, "get_settings", lambda: settings)
        return settings

    yield _install
    fetcher.clear_cache()
    fetcher.set_provider(None)


# ----------------------------------------------------------------- caching


def test_ohlcv_cached_within_ttl(fast_settings) -> None:
    fast_settings(DATA_CACHE_ENABLED=True, OHLCV_CACHE_TTL_SECONDS=1000)
    provider = FakeProvider()
    fetcher.set_provider(provider)

    a = fetcher.fetch_ohlcv("AAPL")
    b = fetcher.fetch_ohlcv("AAPL")
    assert a is not None and b is not None
    assert provider.ohlcv_calls == 1  # second call served from cache


def test_price_cached_within_ttl(fast_settings) -> None:
    fast_settings(DATA_CACHE_ENABLED=True, PRICE_CACHE_TTL_SECONDS=1000)
    provider = FakeProvider(price=123.0)
    fetcher.set_provider(provider)

    assert fetcher.fetch_current_price("AAPL") == 123.0
    assert fetcher.fetch_current_price("AAPL") == 123.0
    assert provider.price_calls == 1


def test_cache_key_is_per_symbol_and_period(fast_settings) -> None:
    fast_settings(DATA_CACHE_ENABLED=True, OHLCV_CACHE_TTL_SECONDS=1000)
    provider = FakeProvider()
    fetcher.set_provider(provider)

    fetcher.fetch_ohlcv("AAPL", period="6mo")
    fetcher.fetch_ohlcv("AAPL", period="1y")
    fetcher.fetch_ohlcv("MSFT", period="6mo")
    assert provider.ohlcv_calls == 3  # three distinct keys


def test_cache_disabled_always_calls_provider(fast_settings) -> None:
    fast_settings(DATA_CACHE_ENABLED=False)
    provider = FakeProvider()
    fetcher.set_provider(provider)

    fetcher.fetch_ohlcv("AAPL")
    fetcher.fetch_ohlcv("AAPL")
    assert provider.ohlcv_calls == 2


def test_ttl_expiry_evicts(fast_settings, monkeypatch) -> None:
    fast_settings(DATA_CACHE_ENABLED=True, OHLCV_CACHE_TTL_SECONDS=100)
    provider = FakeProvider()
    fetcher.set_provider(provider)

    clock = {"t": 1000.0}
    monkeypatch.setattr(fetcher.time, "monotonic", lambda: clock["t"])

    fetcher.fetch_ohlcv("AAPL")
    clock["t"] += 50  # still fresh
    fetcher.fetch_ohlcv("AAPL")
    assert provider.ohlcv_calls == 1
    clock["t"] += 100  # now expired
    fetcher.fetch_ohlcv("AAPL")
    assert provider.ohlcv_calls == 2


# ----------------------------------------------------------------- retry


def test_retry_recovers_from_transient_none(fast_settings) -> None:
    fast_settings(DATA_CACHE_ENABLED=True, FETCH_MAX_RETRIES=3)
    provider = FakeProvider(fail_first=2)  # None, None, then df
    fetcher.set_provider(provider)

    assert fetcher.fetch_ohlcv("AAPL") is not None
    assert provider.ohlcv_calls == 3


def test_retry_recovers_from_exception(fast_settings) -> None:
    fast_settings(DATA_CACHE_ENABLED=True, FETCH_MAX_RETRIES=3)
    provider = FakeProvider(raise_first=1)  # raise once, then succeed
    fetcher.set_provider(provider)

    assert fetcher.fetch_current_price("AAPL") == 100.0
    assert provider.price_calls == 2


def test_retry_exhaustion_returns_none(fast_settings) -> None:
    fast_settings(DATA_CACHE_ENABLED=True, FETCH_MAX_RETRIES=2)
    provider = FakeProvider(fail_first=5)
    fetcher.set_provider(provider)

    assert fetcher.fetch_ohlcv("AAPL") is None
    assert provider.ohlcv_calls == 2  # capped at FETCH_MAX_RETRIES
    # A failure is not cached -> next call retries again.
    assert fetcher.fetch_ohlcv("AAPL") is None
    assert provider.ohlcv_calls == 4


# ----------------------------------------------------------------- cache class


def test_ttl_cache_thread_safe() -> None:
    cache = fetcher._TTLCache()
    errors = []

    def worker(i):
        try:
            for j in range(200):
                cache.set(("k", i, j), j, ttl=100)
                cache.get(("k", i, j))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(cache) == 8 * 200


def test_provider_selection_from_settings(monkeypatch) -> None:
    from data.providers import AlpacaProvider, YFinanceProvider

    fetcher.set_provider(None)
    monkeypatch.setattr(fetcher, "get_settings", lambda: Settings(MARKET_DATA_PROVIDER="yfinance"))
    assert isinstance(fetcher.get_provider(), YFinanceProvider)

    fetcher.set_provider(None)
    monkeypatch.setattr(
        fetcher,
        "get_settings",
        lambda: Settings(
            MARKET_DATA_PROVIDER="alpaca", MARKET_DATA_FALLBACK_PROVIDER=""
        ),
    )
    assert isinstance(fetcher.get_provider(), AlpacaProvider)
    fetcher.set_provider(None)
