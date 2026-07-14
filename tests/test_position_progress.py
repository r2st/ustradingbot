"""Tests for ``progress_to_target_pct`` — the entry→target progress used by the
position *card* view (Phase 1 branding/polish)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
import dashboard.quotes as quotes
from config.settings import Settings
from dashboard.live_router import progress_to_target_pct


# ── Pure-function unit tests ────────────────────────────────────────────────

def test_long_midway_is_fifty_percent():
    # entry 100, target 110, price 105 → halfway.
    assert progress_to_target_pct(100.0, 105.0, 110.0, "long") == 50.0


def test_long_at_entry_is_zero():
    assert progress_to_target_pct(100.0, 100.0, 110.0, "long") == 0.0


def test_long_at_target_is_hundred():
    assert progress_to_target_pct(100.0, 110.0, 110.0, "long") == 100.0


def test_long_spec_example_68_percent():
    # Card mock: entry 313.22, target 331.47, ~68% travelled.
    price = 313.22 + 0.68 * (331.47 - 313.22)
    assert progress_to_target_pct(313.22, price, 331.47, "long") == 68.0


def test_clamped_above_target():
    # Price blew through the target — bar caps at 100, not 150.
    assert progress_to_target_pct(100.0, 115.0, 110.0, "long") == 100.0


def test_clamped_below_entry():
    # Price fell below entry (toward stop) — bar floors at 0.
    assert progress_to_target_pct(100.0, 90.0, 110.0, "long") == 0.0


def test_short_mirrored():
    # Short: entry 100, target 90, price 95 → halfway down = 50%.
    assert progress_to_target_pct(100.0, 95.0, 90.0, "short") == 50.0


def test_short_at_target_is_hundred():
    assert progress_to_target_pct(100.0, 90.0, 90.0, "short") == 100.0


@pytest.mark.parametrize("entry,current,target", [
    (None, 105.0, 110.0),
    (100.0, None, 110.0),
    (100.0, 105.0, None),
])
def test_missing_inputs_return_none(entry, current, target):
    assert progress_to_target_pct(entry, current, target, "long") is None


def test_degenerate_entry_equals_target_returns_none():
    assert progress_to_target_pct(100.0, 100.0, 100.0, "long") is None


def test_non_numeric_returns_none():
    assert progress_to_target_pct("abc", 105.0, 110.0, "long") is None


# ── Integration: field is exposed on the live P&L payload ───────────────────

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


def test_live_pnl_exposes_progress_field(client, env, monkeypatch):
    data_dir, _ = env
    (data_dir / "open_positions.json").write_text(json.dumps({"TEST": {
        "symbol": "TEST", "direction": "long", "entry_price": 100.0,
        "stop_price": 95.0, "target_price": 110.0, "quantity": 10,
        "currency": "USD", "entry_time": "2026-07-06T10:00:00",
    }}), encoding="utf-8")
    monkeypatch.setattr(quotes, "_fetch_price", lambda s: {"TEST": 105.0}.get(s))

    row = client.get("/api/live/pnl").json()["positions"][0]
    assert row["progress_to_target_pct"] == 50.0


def test_live_pnl_progress_none_when_unpriced(client, env, monkeypatch):
    data_dir, _ = env
    (data_dir / "open_positions.json").write_text(json.dumps({"DEAD": {
        "symbol": "DEAD", "direction": "long", "entry_price": 20.0,
        "stop_price": 18.0, "target_price": 25.0, "quantity": 100,
        "currency": "USD", "entry_time": "2026-07-06T10:00:00",
    }}), encoding="utf-8")
    monkeypatch.setattr(quotes, "_fetch_price", lambda s: None)

    row = client.get("/api/live/pnl").json()["positions"][0]
    assert row["progress_to_target_pct"] is None
