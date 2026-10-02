"""Tests for paper⇄live mode switching (dashboard/mode_control.py + API)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
from config.settings import Settings
from dashboard.mode_control import (
    consume_restart_request,
    switch_mode,
    update_env_var,
)


# --------------------------------------------------------------------------- #
# update_env_var
# --------------------------------------------------------------------------- #


def test_update_env_var_replaces_existing(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("BROKER=paper\nIBKR_PORT=7497\nLOG_LEVEL=INFO\n")
    update_env_var(env, {"BROKER": "ibkr", "IBKR_PORT": "7496"})
    text = env.read_text()
    assert "BROKER=ibkr" in text
    assert "IBKR_PORT=7496" in text
    assert "LOG_LEVEL=INFO" in text  # untouched


def test_update_env_var_appends_missing(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("LOG_LEVEL=INFO\n")
    update_env_var(env, {"BROKER": "ibkr"})
    assert "BROKER=ibkr" in env.read_text()
    assert "LOG_LEVEL=INFO" in env.read_text()


def test_update_env_var_creates_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    update_env_var(env, {"BROKER": "paper"})
    assert env.exists()
    assert "BROKER=paper" in env.read_text()


def test_update_env_var_preserves_comments(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# my config\nBROKER=paper\n")
    update_env_var(env, {"BROKER": "ibkr"})
    assert "# my config" in env.read_text()


# --------------------------------------------------------------------------- #
# switch_mode
# --------------------------------------------------------------------------- #


def test_switch_to_live_requires_password(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    s = Settings(DATA_DIR=tmp_path, DASHBOARD_PASSWORD="secret")
    res = switch_mode("live", "wrong", s, env_path=env)
    assert res.ok is False
    assert "Invalid admin password" in res.message
    assert not env.exists()  # nothing written


def test_switch_to_live_with_correct_password(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    s = Settings(DATA_DIR=tmp_path, DASHBOARD_PASSWORD="secret")
    res = switch_mode("live", "secret", s, env_path=env)
    assert res.ok is True
    assert res.mode == "LIVE"
    assert res.restart_requested is True
    text = env.read_text()
    assert "BROKER=ibkr" in text
    assert "IBKR_PORT=7496" in text
    # Restart sentinel dropped.
    assert consume_restart_request(tmp_path) is True


def test_switch_to_live_refused_without_password_configured(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    s = Settings(DATA_DIR=tmp_path, DASHBOARD_PASSWORD="")
    res = switch_mode("live", "", s, env_path=env)
    assert res.ok is False
    assert "no admin password" in res.message.lower()


def test_switch_to_paper_needs_no_password(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    s = Settings(DATA_DIR=tmp_path, BROKER="ibkr", IBKR_PORT=7496,
                 DASHBOARD_PASSWORD="secret")
    res = switch_mode("paper", "", s, env_path=env)
    assert res.ok is True
    assert res.mode == "PAPER"
    assert "BROKER=paper" in env.read_text()


def test_switch_disabled(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    s = Settings(DATA_DIR=tmp_path, ALLOW_MODE_SWITCH=False, DASHBOARD_PASSWORD="x")
    res = switch_mode("live", "x", s, env_path=env)
    assert res.ok is False
    assert "disabled" in res.message.lower()


def test_switch_unknown_target(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    s = Settings(DATA_DIR=tmp_path, DASHBOARD_PASSWORD="x")
    res = switch_mode("banana", "x", s, env_path=env)
    assert res.ok is False


def test_consume_restart_request_once(tmp_path: Path) -> None:
    from dashboard.mode_control import request_restart

    request_restart(tmp_path, "live")
    assert consume_restart_request(tmp_path) is True
    assert consume_restart_request(tmp_path) is False  # consumed


# --------------------------------------------------------------------------- #
# API endpoint
# --------------------------------------------------------------------------- #


@pytest.fixture
def client() -> TestClient:
    return TestClient(dash.app, raise_server_exceptions=False)


def _use(monkeypatch, tmp_path, **kw) -> None:
    settings = Settings(DASHBOARD_AUTH_ENABLED=False, DATA_DIR=tmp_path, **kw)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)


def test_api_switch_to_live_bad_password(client, monkeypatch, tmp_path) -> None:
    _use(monkeypatch, tmp_path, DASHBOARD_PASSWORD="secret")
    # Point the env write at a temp file to avoid touching the real .env.
    monkeypatch.setattr(
        "dashboard.mode_control._project_root", lambda: tmp_path
    )
    resp = client.post("/api/mode/switch",
                       json={"target": "live", "admin_password": "nope"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_api_switch_to_paper_ok(client, monkeypatch, tmp_path) -> None:
    _use(monkeypatch, tmp_path, BROKER="ibkr", IBKR_PORT=7496,
         DASHBOARD_PASSWORD="secret")
    monkeypatch.setattr("dashboard.mode_control._project_root", lambda: tmp_path)
    resp = client.post("/api/mode/switch", json={"target": "paper"})
    body = resp.json()
    assert body["ok"] is True
    assert body["mode"] == "PAPER"
    assert (tmp_path / ".env").read_text().count("BROKER=paper") == 1


def test_dashboard_renders_toggle_button(client, monkeypatch, tmp_path) -> None:
    _use(monkeypatch, tmp_path)
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Switch to Live" in resp.text
    assert "REAL money" in resp.text  # confirmation dialog copy
