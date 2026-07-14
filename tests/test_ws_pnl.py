"""Tests for the Phase 2 real-time P&L WebSocket (``/ws/pnl``) and its
signed connect-token endpoint (``/api/ws/token``)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import dashboard.app as dash
import dashboard.push as push
import dashboard.quotes as quotes
import dashboard.ws_pnl as ws_pnl
from config.settings import Settings


@pytest.fixture
def client() -> TestClient:
    return TestClient(dash.app, raise_server_exceptions=False)


@pytest.fixture
def env(monkeypatch, tmp_path):
    data_dir = tmp_path / "data_store"
    data_dir.mkdir()
    settings = Settings(
        DASHBOARD_AUTH_ENABLED=False, DATA_DIR=data_dir, TOTAL_CAPITAL=12_000.0
    )
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    push._STORES.clear()
    quotes.clear_cache()
    yield data_dir, settings
    quotes.clear_cache()
    push._STORES.clear()


@pytest.fixture
def auth_env(monkeypatch, tmp_path):
    data_dir = tmp_path / "data_store"
    data_dir.mkdir()
    settings = Settings(
        DASHBOARD_AUTH_ENABLED=True,
        DASHBOARD_USERNAME="admin",
        DASHBOARD_PASSWORD="pw",
        DATA_DIR=data_dir,
        TOTAL_CAPITAL=12_000.0,
    )
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    push._STORES.clear()
    quotes.clear_cache()
    yield data_dir, settings
    quotes.clear_cache()
    push._STORES.clear()


def _write_positions(data_dir: Path, positions: dict) -> None:
    (data_dir / "open_positions.json").write_text(
        json.dumps(positions), encoding="utf-8"
    )


def _set_prices(monkeypatch, prices: dict) -> None:
    monkeypatch.setattr(quotes, "_fetch_price", lambda s: prices.get(s))


# ------------------------------------------------------------- token unit


def test_token_roundtrip_and_expiry():
    settings = Settings(DASHBOARD_PASSWORD="secret")
    tok = ws_pnl.make_ws_token(settings, now=1000.0)
    assert ws_pnl.verify_ws_token(settings, tok, now=1000.0) is True
    # Within the TTL window.
    assert ws_pnl.verify_ws_token(settings, tok, now=1000.0 + 100) is True
    # Past the TTL window.
    assert ws_pnl.verify_ws_token(
        settings, tok, now=1000.0 + ws_pnl.TOKEN_TTL_SECONDS + 1
    ) is False
    # Tampered signature / malformed.
    assert ws_pnl.verify_ws_token(settings, "1000.deadbeef", now=1000.0) is False
    assert ws_pnl.verify_ws_token(settings, "garbage", now=1000.0) is False
    # A token signed with a different password does not verify.
    other = Settings(DASHBOARD_PASSWORD="different")
    assert ws_pnl.verify_ws_token(other, tok, now=1000.0) is False


# --------------------------------------------------------- streaming (no auth)


def test_ws_streams_pnl_frame(client, env, monkeypatch):
    data_dir, _ = env
    _write_positions(data_dir, {"TEST": {
        "symbol": "TEST", "direction": "long", "entry_price": 100.0,
        "stop_price": 95.0, "target_price": 110.0, "quantity": 10,
        "currency": "USD", "entry_time": "2026-07-06T10:00:00",
    }})
    _set_prices(monkeypatch, {"TEST": 104.0})

    with client.websocket_connect("/ws/pnl") as ws:
        frame = ws.receive_json()

    assert frame["type"] == "pnl"
    assert "market_open" in frame
    assert frame["totals"]["unrealized"] == 40.0
    assert frame["totals"]["account_equity"] == 12_040.0
    assert frame["positions"][0]["symbol"] == "TEST"


def test_ws_market_open_flag_reflects_clock(client, env, monkeypatch):
    monkeypatch.setattr(ws_pnl, "_market_open", lambda settings: True)
    with client.websocket_connect("/ws/pnl") as ws:
        assert ws.receive_json()["market_open"] is True
    monkeypatch.setattr(ws_pnl, "_market_open", lambda settings: False)
    with client.websocket_connect("/ws/pnl") as ws:
        assert ws.receive_json()["market_open"] is False


def test_ws_matches_rest_snapshot(client, env, monkeypatch):
    """The socket frame carries the same totals as GET /api/live/pnl."""
    data_dir, _ = env
    _write_positions(data_dir, {"AAA": {
        "symbol": "AAA", "direction": "long", "entry_price": 50.0,
        "stop_price": 45.0, "target_price": 60.0, "quantity": 20,
        "currency": "USD", "entry_time": "2026-07-06T10:00:00",
    }})
    _set_prices(monkeypatch, {"AAA": 55.0})

    rest = client.get("/api/live/pnl").json()
    with client.websocket_connect("/ws/pnl") as ws:
        frame = ws.receive_json()
    assert frame["totals"]["unrealized"] == rest["totals"]["unrealized"]
    assert frame["positions"][0]["unrealized_pnl"] == rest["positions"][0]["unrealized_pnl"]


# ------------------------------------------------------------------- auth


def test_ws_token_endpoint_requires_auth(client, auth_env):
    assert client.get("/api/ws/token").status_code == 401
    r = client.get("/api/ws/token", auth=("admin", "pw"))
    assert r.status_code == 200
    assert r.json()["token"]


def test_ws_rejects_without_credentials(client, auth_env):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/pnl") as ws:
            ws.receive_json()
    assert exc.value.code == ws_pnl._WS_CLOSE_POLICY


def test_ws_rejects_bad_token(client, auth_env):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/pnl?token=1.deadbeef") as ws:
            ws.receive_json()


def test_ws_accepts_valid_token(client, auth_env):
    tok = client.get("/api/ws/token", auth=("admin", "pw")).json()["token"]
    with client.websocket_connect(f"/ws/pnl?token={tok}") as ws:
        assert ws.receive_json()["type"] == "pnl"


def test_ws_accepts_basic_auth_header(client, auth_env):
    hdr = {"authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
    with client.websocket_connect("/ws/pnl", headers=hdr) as ws:
        assert ws.receive_json()["type"] == "pnl"


def test_ws_rejects_wrong_basic_auth(client, auth_env):
    hdr = {"authorization": "Basic " + base64.b64encode(b"admin:nope").decode()}
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/pnl", headers=hdr) as ws:
            ws.receive_json()
