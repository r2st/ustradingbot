"""Tests for the live P&L + position-monitoring endpoint (monitoring F1 + F3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
import dashboard.quotes as quotes
from config.settings import Settings


@pytest.fixture
def client() -> TestClient:
    return TestClient(dash.app, raise_server_exceptions=False)


@pytest.fixture
def env(monkeypatch, tmp_path):
    data_dir = tmp_path / "data_store"
    data_dir.mkdir()
    settings = Settings(DASHBOARD_AUTH_ENABLED=False, DATA_DIR=data_dir,
                        TOTAL_CAPITAL=12_000.0)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    quotes.clear_cache()
    yield data_dir, settings
    quotes.clear_cache()


def _write_positions(data_dir: Path, positions: dict) -> None:
    (data_dir / "open_positions.json").write_text(
        json.dumps(positions), encoding="utf-8"
    )


def _set_prices(monkeypatch, prices: dict) -> None:
    monkeypatch.setattr(quotes, "_fetch_price", lambda s: prices.get(s))


def test_long_position_math_matches_spec(client, env, monkeypatch):
    """Spec F3: entry 100, stop 95, target 110, price 104."""
    data_dir, _ = env
    _write_positions(data_dir, {"TEST": {
        "symbol": "TEST", "direction": "long", "entry_price": 100.0,
        "stop_price": 95.0, "target_price": 110.0, "quantity": 10,
        "currency": "USD", "entry_time": "2026-07-06T10:00:00",
    }})
    _set_prices(monkeypatch, {"TEST": 104.0})

    d = client.get("/api/live/pnl").json()
    row = d["positions"][0]
    assert row["current_price"] == 104.0
    assert row["unrealized_pnl"] == 40.0
    assert row["unrealized_pct"] == 4.0
    assert abs(row["distance_to_stop_pct"] - 8.65) < 0.01
    assert abs(row["distance_to_target_pct"] - 5.77) < 0.01
    assert row["r_progress"] == 0.8
    assert row["proximity"] is None
    assert d["totals"]["unrealized"] == 40.0
    # Equity reconciles: capital + realized (0) + unrealized.
    assert d["totals"]["account_equity"] == 12_040.0


def test_short_position_sign_inverted(client, env, monkeypatch):
    data_dir, _ = env
    _write_positions(data_dir, {"SHRT": {
        "symbol": "SHRT", "direction": "short", "entry_price": 100.0,
        "stop_price": 105.0, "target_price": 90.0, "quantity": 10,
        "currency": "USD", "entry_time": "2026-07-06T10:00:00",
    }})
    _set_prices(monkeypatch, {"SHRT": 96.0})  # price dropped 4 → short is UP

    row = client.get("/api/live/pnl").json()["positions"][0]
    assert row["side"] == "short"
    assert row["unrealized_pnl"] == 40.0
    assert row["r_progress"] == 0.8  # risk = 5/share, move = +4 in trade direction


def test_missing_quote_is_stale_not_zero(client, env, monkeypatch):
    data_dir, _ = env
    _write_positions(data_dir, {
        "GOOD": {"symbol": "GOOD", "entry_price": 50.0, "stop_price": 45.0,
                 "target_price": 60.0, "quantity": 10, "currency": "USD",
                 "entry_time": "2026-07-06T10:00:00"},
        "DEAD": {"symbol": "DEAD", "entry_price": 20.0, "stop_price": 18.0,
                 "target_price": 25.0, "quantity": 100, "currency": "USD",
                 "entry_time": "2026-07-06T10:00:00"},
    })
    _set_prices(monkeypatch, {"GOOD": 55.0})

    d = client.get("/api/live/pnl").json()
    by_sym = {p["symbol"]: p for p in d["positions"]}
    assert by_sym["DEAD"]["stale"] is True
    assert by_sym["DEAD"]["current_price"] is None
    assert by_sym["DEAD"]["unrealized_pnl"] is None
    # Totals only include the priced symbol — the dead one never zeroes them.
    assert d["totals"]["unrealized"] == 50.0
    assert d["totals"]["positions_priced"] == 1
    assert d["totals"]["positions_total"] == 2


def test_proximity_flag_near_stop(client, env, monkeypatch):
    data_dir, _ = env
    _write_positions(data_dir, {"NR": {
        "symbol": "NR", "entry_price": 100.0, "stop_price": 99.5,
        "target_price": 120.0, "quantity": 1, "currency": "USD",
        "entry_time": "2026-07-06T10:00:00",
    }})
    _set_prices(monkeypatch, {"NR": 100.0})  # 0.5% from stop < 1% default

    row = client.get("/api/live/pnl").json()["positions"][0]
    assert row["proximity"] == "near_stop"


def test_empty_book(client, env):
    d = client.get("/api/live/pnl").json()
    assert d["positions"] == []
    assert d["totals"]["unrealized"] == 0.0


def test_intraday_sampling_and_readback(client, env, monkeypatch):
    data_dir, _ = env
    _write_positions(data_dir, {"TEST": {
        "symbol": "TEST", "entry_price": 100.0, "stop_price": 95.0,
        "target_price": 110.0, "quantity": 10, "currency": "USD",
        "entry_time": "2026-07-06T10:00:00",
    }})
    _set_prices(monkeypatch, {"TEST": 104.0})
    # Force the throttle open.
    import dashboard.live_router as lr

    lr._last_sample_monotonic = 0.0
    client.get("/api/live/pnl")
    d = client.get("/api/live/pnl/intraday").json()
    assert len(d["points"]) == 1
    assert d["points"][0]["total"] == 40.0
    # A second hit inside the throttle window adds no point.
    client.get("/api/live/pnl")
    d = client.get("/api/live/pnl/intraday").json()
    assert len(d["points"]) == 1


def test_laddered_manual_position_effective_levels(client, env, monkeypatch):
    data_dir, _ = env
    _write_positions(data_dir, {"LAD": {
        "symbol": "LAD", "direction": "long", "manual": True,
        "entry_price": 100.0, "stop_price": 90.0, "target_price": 120.0,
        "quantity": 30, "currency": "USD",
        "entry_time": "2026-07-06T10:00:00",
        "levels": [
            {"kind": "stop", "price": 97.0, "quantity": 10, "triggered": False},
            {"kind": "stop", "price": 95.0, "quantity": 20, "triggered": False},
            {"kind": "target", "price": 105.0, "quantity": 10, "triggered": True},
            {"kind": "target", "price": 110.0, "quantity": 20, "triggered": False},
        ],
    }})
    _set_prices(monkeypatch, {"LAD": 101.0})

    row = client.get("/api/live/pnl").json()["positions"][0]
    # Nearest untriggered rungs, not the display mirror.
    assert row["stop_price"] == 97.0
    assert row["target_price"] == 110.0
    assert row["next_level"] is not None
