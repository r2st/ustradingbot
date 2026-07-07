"""Tests for the TA chart API (dashboard/ta_router, TA1)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
import dashboard.ta_router as ta
from config.settings import Settings
from journal.rationale import RationaleStore
from signals.indicator_snapshot import build_indicator_snapshot
from signals.rsi_signals import calculate_rsi
from signals.signal_types import Grade, Signal


@pytest.fixture
def client() -> TestClient:
    return TestClient(dash.app, raise_server_exceptions=False)


@pytest.fixture
def env(monkeypatch, tmp_path):
    data_dir = tmp_path / "data_store"
    data_dir.mkdir()
    settings = Settings(DASHBOARD_AUTH_ENABLED=False, DATA_DIR=data_dir)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    # The router caches are module-level: isolate every test.
    ta._chart_cache.clear()
    ta._indicator_cache.clear()
    return data_dir


@pytest.fixture
def no_network(monkeypatch, bullish_df):
    """Serve the fixture df instead of hitting providers, and stub quotes."""
    import data.fetcher as fetcher

    monkeypatch.setattr(
        fetcher, "fetch_ohlcv", lambda symbol, period="6mo": bullish_df
    )
    from dashboard import quotes

    monkeypatch.setattr(
        quotes,
        "get_quote",
        lambda symbol, include_prev_close=False: {
            "price": float(bullish_df["Close"].iloc[-1]),
            "change_pct": 1.2,
        },
    )


def _signal(**kw) -> Signal:
    defaults = dict(
        symbol="AAPL",
        strategy="momentum",
        entry_price=148.60,
        stop_price=142.30,
        target_price=159.94,
        signal_strength=0.81,
        grade=Grade.A,
        rsi_value=61.0,
        volume_ratio=2.1,
    )
    defaults.update(kw)
    return Signal(**defaults)


def _seed_rationale(
    data_dir: Path, df: pd.DataFrame, with_indicators: bool = True
) -> None:
    snap = build_indicator_snapshot(df)
    assert snap is not None
    store = RationaleStore(data_dir)
    store.record(
        _signal(),
        [{"key": "ai_veto", "name": "AI veto score", "score": 9.0,
          "explanation": "APPROVE — no negative news"}],
        quantity=33,
        entry_price=148.60,
        entry_time="2026-07-01T10:30:00",
        bars=snap["bars"],
        indicators=snap if with_indicators else None,
    )


def _seed_open_position(data_dir: Path, symbol: str = "AAPL") -> None:
    (data_dir / "open_positions.json").write_text(json.dumps({
        symbol: {
            "symbol": symbol, "strategy": "momentum", "grade": "A",
            "direction": "long", "quantity": 33,
            "entry_price": 148.60, "stop_price": 142.30,
            "target_price": 159.94, "entry_time": "2026-07-01T10:30:00",
        }
    }), encoding="utf-8")


# --------------------------------------------------------------------------- #
# ta-chart: snapshot path
# --------------------------------------------------------------------------- #


def test_ta_chart_serves_persisted_snapshot(client, env, bullish_df) -> None:
    _seed_rationale(env, bullish_df)
    resp = client.get("/api/trade/AAPL/ta-chart")
    assert resp.status_code == 200
    d = resp.json()
    assert d["source"] == "snapshot"
    assert d["symbol"] == "AAPL"
    assert len(d["bars"]) == len(d["series"]["rsi"])
    for key in ("ema9", "ema20", "ema50", "ema200", "rsi", "macd",
                "macd_signal", "macd_hist", "bb_up", "bb_mid", "bb_lo",
                "obv", "vol_avg20", "atr14"):
        assert key in d["series"], key
    assert d["trade"]["entry_price"] == 148.60
    assert d["trade"]["quantity"] == 33
    assert d["annotations"]["entry_bar"] is not None
    assert d["annotations"]["atr14"] is not None
    assert "support" in d["levels"] and "resistance" in d["levels"]


def test_ta_chart_snapshot_is_stable_across_reloads(client, env, bullish_df) -> None:
    """Snapshot records must render identically with zero provider calls."""
    _seed_rationale(env, bullish_df)
    import data.fetcher as fetcher

    def boom(*a, **k):  # any provider call is a failure of the snapshot path
        raise AssertionError("snapshot path must not fetch bars")

    first = client.get("/api/trade/AAPL/ta-chart").json()
    original = fetcher.fetch_ohlcv
    fetcher.fetch_ohlcv = boom
    try:
        second = client.get("/api/trade/AAPL/ta-chart").json()
    finally:
        fetcher.fetch_ohlcv = original
    assert first["bars"] == second["bars"]
    assert first["series"] == second["series"]


def test_ta_chart_explanation_matches_persisted_values(
    client, env, bullish_df
) -> None:
    _seed_rationale(env, bullish_df)
    d = client.get("/api/trade/AAPL/ta-chart").json()
    ex = d["explanation"]
    assert "grade A" in ex["entry"]
    assert "$142.30" in ex["stop"]
    assert "$159.94" in ex["target"]
    assert "trend-continuation" in ex["setup"]
    assert ex["ai_note"] == "AI veto: APPROVE — no negative news"
    assert ex["current"] is None  # closed trade: no live block


# --------------------------------------------------------------------------- #
# ta-chart: recompute path
# --------------------------------------------------------------------------- #


def test_ta_chart_recomputes_for_pre_ta1_trades(
    client, env, bullish_df, no_network
) -> None:
    _seed_rationale(env, bullish_df, with_indicators=False)  # v1 record
    resp = client.get("/api/trade/AAPL/ta-chart")
    assert resp.status_code == 200
    d = resp.json()
    assert d["source"] == "recomputed"
    assert d["series"]["rsi"][-1] is not None


def test_ta_chart_open_position_has_current_readings(
    client, env, bullish_df, no_network
) -> None:
    _seed_open_position(env)
    resp = client.get("/api/trade/AAPL/ta-chart")
    assert resp.status_code == 200
    d = resp.json()
    assert d["source"] == "recomputed"
    assert d["trade"]["open"] is True
    assert d["live"] is not None
    assert d["explanation"]["current"], "open positions must show Now: readings"
    assert d["explanation"]["current"].startswith("Now: ")


def test_ta_chart_404_when_no_trade_exists(client, env, monkeypatch) -> None:
    import data.fetcher as fetcher

    monkeypatch.setattr(fetcher, "fetch_ohlcv", lambda *a, **k: None)
    resp = client.get("/api/trade/ZZZZ/ta-chart")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# indicators endpoint
# --------------------------------------------------------------------------- #


def test_live_indicators_endpoint(client, env, bullish_df, no_network) -> None:
    resp = client.get("/api/trade/AAPL/indicators")
    assert resp.status_code == 200
    d = resp.json()
    expected_rsi = calculate_rsi(bullish_df).rsi_value
    assert d["rsi"] == pytest.approx(expected_rsi, abs=0.05)
    for key in ("price", "macd", "macd_hist", "ema20", "ema200",
                "atr14", "bb_up", "bb_lo", "levels", "as_of"):
        assert key in d, key
    assert d["price"] == pytest.approx(
        float(bullish_df["Close"].iloc[-1]), abs=0.01
    )


def test_live_indicators_cached_between_calls(
    client, env, bullish_df, monkeypatch
) -> None:
    import data.fetcher as fetcher

    calls = {"n": 0}

    def counting_fetch(symbol, period="6mo"):
        calls["n"] += 1
        return bullish_df

    monkeypatch.setattr(fetcher, "fetch_ohlcv", counting_fetch)
    from dashboard import quotes

    monkeypatch.setattr(
        quotes, "get_quote",
        lambda symbol, include_prev_close=False: {"price": None},
    )
    assert client.get("/api/trade/AAPL/indicators").status_code == 200
    assert client.get("/api/trade/AAPL/indicators").status_code == 200
    assert calls["n"] == 1  # second hit served from the TTL cache


def test_live_indicators_404_without_data(client, env, monkeypatch) -> None:
    import data.fetcher as fetcher

    monkeypatch.setattr(fetcher, "fetch_ohlcv", lambda *a, **k: None)
    resp = client.get("/api/trade/NOPE/indicators")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# rationale endpoint stays lean (v2 key stripped)
# --------------------------------------------------------------------------- #


def test_rationale_endpoint_strips_indicator_series(
    client, env, bullish_df
) -> None:
    _seed_rationale(env, bullish_df)
    d = client.get("/api/rationale?symbol=AAPL").json()
    assert d["record"] is not None
    assert "indicators" not in d["record"]
    assert d["record"]["bars"]  # F9 chart bars still served


def test_rationale_record_persists_indicators_key(env, bullish_df) -> None:
    from journal.rationale import find_rationale

    _seed_rationale(env, bullish_df)
    rec = find_rationale(env, "AAPL")
    assert rec is not None
    ind = rec.get("indicators")
    assert ind is not None
    assert "bars" not in ind  # top-level bars remain the single candle source
    assert len(ind["series"]["rsi"]) == len(rec["bars"])
