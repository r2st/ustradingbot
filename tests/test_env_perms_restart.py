"""
Tests for the .env secret-file permissions and the restart handshake (B-6).
"""

from __future__ import annotations

import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from dashboard.mode_control import (
    consume_restart_request,
    request_restart,
    restart_status,
    update_env_var,
)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_update_env_var_sets_0600(tmp_path):
    env = tmp_path / ".env"
    update_env_var(env, {"BROKER": "paper"})
    mode = stat.S_IMODE(env.stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_restart_ack_roundtrip(tmp_path):
    data_dir = tmp_path / "data_store"
    data_dir.mkdir()
    # Nothing requested yet.
    assert restart_status(data_dir) == {"pending": False, "acked_at": None, "target": None}

    request_restart(data_dir, "live")
    status = restart_status(data_dir)
    assert status["pending"] is True  # sentinel present, not yet consumed

    # Engine consumes it → ack written, no longer pending.
    assert consume_restart_request(data_dir) is True
    status = restart_status(data_dir)
    assert status["pending"] is False
    assert status["acked_at"]
    assert status["target"] == "live"

    # A second consume with no sentinel is a no-op.
    assert consume_restart_request(data_dir) is False


def test_ack_file_is_valid_json(tmp_path):
    data_dir = tmp_path / "data_store"
    data_dir.mkdir()
    request_restart(data_dir, "provider:alpaca")
    consume_restart_request(data_dir)
    ack = json.loads((data_dir / "restart_ack.json").read_text())
    assert ack["target"] == "provider:alpaca"
    assert "acked_at" in ack


def test_restart_status_endpoint(monkeypatch, tmp_path):
    data_dir = tmp_path / "data_store"
    data_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "False")
    monkeypatch.setenv("TOTAL_CAPITAL", "10000")
    from config.settings import get_settings

    get_settings.cache_clear()
    import dashboard.app as dash

    client = TestClient(dash.app, raise_server_exceptions=False)
    resp = client.get("/api/engine/restart-status")
    assert resp.status_code == 200
    assert resp.json()["pending"] is False

    request_restart(data_dir, "live")
    assert client.get("/api/engine/restart-status").json()["pending"] is True
