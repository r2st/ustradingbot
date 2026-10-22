"""Tests for the dashboard summary card endpoint (GET /api/live/summary)."""

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
                        TOTAL_CAPITAL=10_000.0)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    quotes.clear_cache()
    yield data_dir
    quotes.clear_cache()


def _pos(symbol: str, entry: float, qty: int) -> dict:
    return {"symbol": symbol, "direction": "long", "entry_price": entry,
            "stop_price": entry * 0.9, "target_price": entry * 1.2,
            "quantity": qty, "currency": "USD", "entry_time": "2026-07-06T10:00:00"}


def _write(data_dir: Path, positions: dict) -> None:
    (data_dir / "open_positions.json").write_text(json.dumps(positions), encoding="utf-8")


def test_summary_value_day_pnl_and_top_holdings(client, env, monkeypatch):
    _write(env, {
        "AAA": _pos("AAA", 100.0, 10),   # → 110 : +100, value 1100
        "BBB": _pos("BBB", 50.0, 40),    # → 45  : -200, value 1800
        "CCC": _pos("CCC", 20.0, 10),    # → 20  :    0, value  200
    })
    monkeypatch.setattr(quotes, "_fetch_price",
                        lambda s: {"AAA": 110.0, "BBB": 45.0, "CCC": 20.0}[s])

    d = client.get("/api/live/summary").json()
    assert d["account_equity"] == 9_900.0           # 10k - 100 unrealized
    assert d["day_pnl"] == -100.0
    assert d["day_pnl_pct"] == -1.0                 # vs opening value 10k
    assert d["invested"] == 3_100.0
    assert d["cash"] == 6_800.0
    assert d["positions_total"] == 3
    assert [h["symbol"] for h in d["holdings"]] == ["BBB", "AAA", "CCC"]
    top = d["holdings"][0]
    assert top["market_value"] == 1_800.0
    assert top["weight_pct"] == pytest.approx(18.18, abs=0.01)
    assert top["unrealized_pct"] == -10.0


def test_summary_limits_to_top_five(client, env, monkeypatch):
    _write(env, {f"S{i}": _pos(f"S{i}", 10.0 + i, 10) for i in range(8)})
    monkeypatch.setattr(quotes, "_fetch_price", lambda s: None)  # all stale

    d = client.get("/api/live/summary").json()
    assert len(d["holdings"]) == 5
    # Stale rows size off cost basis, largest first.
    assert d["holdings"][0]["symbol"] == "S7"
    assert d["holdings"][0]["stale"] is True


def test_summary_empty_book(client, env):
    d = client.get("/api/live/summary").json()
    assert d["holdings"] == []
    assert d["account_equity"] == 10_000.0
    assert d["day_pnl_pct"] == 0.0


def test_summary_requires_auth(client, monkeypatch, tmp_path):
    settings = Settings(DASHBOARD_AUTH_ENABLED=True, DASHBOARD_PASSWORD="pw",
                        DATA_DIR=tmp_path)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    assert client.get("/api/live/summary").status_code == 401


def test_dashboard_renders_holdings_panel(client, env):
    html = client.get("/dashboard").text
    assert 'id="ovHoldingsList"' in html
    assert "Portfolio Value" in html
    assert "/api/live/summary" in html
