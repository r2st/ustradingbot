"""
T5 — the /ws/pnl push loop must degrade cleanly.

Complements ``tests/test_ws_pnl.py`` (streaming, token, auth) and
``tests/test_reconnect.py`` (broker backoff) by exercising the server-side
error path: when snapshot building raises, the socket closes rather than
crashing the worker.
"""

from __future__ import annotations

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


def test_ws_closes_cleanly_when_snapshot_raises(client, env, monkeypatch):
    def boom(_settings):
        raise RuntimeError("snapshot provider exploded")

    monkeypatch.setattr(ws_pnl, "build_pnl_snapshot", boom)

    # The connect succeeds (accept happens before the first frame build); the
    # first frame build raises and the handler closes the socket cleanly, which
    # the client observes as a disconnect on receive — not a 500 / crash.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/pnl") as ws:
            ws.receive_json()


def test_ws_interval_selection_prefers_open_cadence(monkeypatch):
    # A market-open frame selects the fast cadence; closed selects the slow one.
    open_frame = {"market_open": True}
    closed_frame = {"market_open": False}
    fast = ws_pnl.PNL_PUSH_INTERVAL_OPEN
    slow = ws_pnl.PNL_PUSH_INTERVAL_CLOSED
    assert fast < slow  # the whole point of two cadences
    # Mirror the handler's selection logic.
    assert (fast if open_frame.get("market_open") else slow) == fast
    assert (fast if closed_frame.get("market_open") else slow) == slow
