"""Tests for the shared dashboard quote service (monitoring F1)."""

from __future__ import annotations

import pandas as pd
import pytest

import dashboard.quotes as quotes


@pytest.fixture(autouse=True)
def _fresh_cache():
    quotes.clear_cache()
    yield
    quotes.clear_cache()


def test_batch_quotes_and_caching(monkeypatch):
    calls = []

    def fake_price(symbol):
        calls.append(symbol)
        return {"NVDA": 150.0, "AAPL": 200.0}.get(symbol)

    monkeypatch.setattr(quotes, "_fetch_price", fake_price)
    out = quotes.get_quotes(["NVDA", "AAPL", "NVDA"])
    assert out["NVDA"]["price"] == 150.0
    assert out["AAPL"]["price"] == 200.0
    assert calls == ["NVDA", "AAPL"]  # deduped

    # Second call within the TTL: served entirely from cache.
    out2 = quotes.get_quotes(["NVDA", "AAPL"])
    assert calls == ["NVDA", "AAPL"]
    assert out2["NVDA"]["price"] == 150.0


def test_failed_symbol_is_none_not_zero(monkeypatch):
    def fake_price(symbol):
        if symbol == "BAD":
            raise RuntimeError("boom")
        return 10.0

    monkeypatch.setattr(quotes, "_fetch_price", fake_price)
    out = quotes.get_quotes(["GOOD", "BAD"])
    assert out["GOOD"]["price"] == 10.0
    assert out["BAD"]["price"] is None  # stale, never poisons totals


def test_failure_is_cached_briefly(monkeypatch):
    calls = []

    def fake_price(symbol):
        calls.append(symbol)
        return None

    monkeypatch.setattr(quotes, "_fetch_price", fake_price)
    quotes.get_quotes(["DEAD"])
    quotes.get_quotes(["DEAD"])
    assert calls == ["DEAD"]  # not re-fetched on every poll tick


def test_prev_close_and_day_change(monkeypatch):
    df = pd.DataFrame({"Close": [98.0, 100.0, 105.0]})
    monkeypatch.setattr(quotes, "_fetch_price", lambda s: 105.0)
    monkeypatch.setattr(quotes, "_fetch_ohlcv", lambda s: df)
    out = quotes.get_quotes(["NVDA"], include_prev_close=True)
    q = out["NVDA"]
    assert q["prev_close"] == 100.0
    assert q["change_pct"] == 5.0


def test_prev_close_failure_degrades(monkeypatch):
    monkeypatch.setattr(quotes, "_fetch_price", lambda s: 105.0)
    monkeypatch.setattr(quotes, "_fetch_ohlcv",
                        lambda s: (_ for _ in ()).throw(RuntimeError("x")))
    q = quotes.get_quotes(["NVDA"], include_prev_close=True)["NVDA"]
    assert q["price"] == 105.0
    assert q["prev_close"] is None
    assert q["change_pct"] is None
