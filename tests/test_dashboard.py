"""Tests for the dashboard HTTP Basic Auth (P0 fix).

Verifies that protected pages require valid credentials, that ``/health``
stays public for liveness probes, that a missing password fails closed, and
that auth can be disabled for local development.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
from config.settings import Settings


@pytest.fixture
def client() -> TestClient:
    return TestClient(dash.app, raise_server_exceptions=False)


def _use_settings(monkeypatch, **kwargs) -> None:
    """Point the dashboard's get_settings() at a controlled Settings object."""
    settings = Settings(**kwargs)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)


def _basic_header(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# --------------------------------------------------------------------------- #
# /health is always public
# --------------------------------------------------------------------------- #


def test_health_is_public(client: TestClient, monkeypatch) -> None:
    _use_settings(monkeypatch, DASHBOARD_AUTH_ENABLED=True, DASHBOARD_PASSWORD="secret")
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# --------------------------------------------------------------------------- #
# protected dashboard route
# --------------------------------------------------------------------------- #


def test_dashboard_requires_credentials(client: TestClient, monkeypatch) -> None:
    _use_settings(monkeypatch, DASHBOARD_AUTH_ENABLED=True, DASHBOARD_PASSWORD="secret")
    resp = client.get("/dashboard")
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == "Basic"


def test_dashboard_rejects_wrong_password(client: TestClient, monkeypatch) -> None:
    _use_settings(
        monkeypatch,
        DASHBOARD_AUTH_ENABLED=True,
        DASHBOARD_USERNAME="admin",
        DASHBOARD_PASSWORD="secret",
    )
    resp = client.get("/dashboard", headers=_basic_header("admin", "wrong"))
    assert resp.status_code == 401


def test_dashboard_rejects_wrong_username(client: TestClient, monkeypatch) -> None:
    _use_settings(
        monkeypatch,
        DASHBOARD_AUTH_ENABLED=True,
        DASHBOARD_USERNAME="admin",
        DASHBOARD_PASSWORD="secret",
    )
    resp = client.get("/dashboard", headers=_basic_header("intruder", "secret"))
    assert resp.status_code == 401


def test_dashboard_accepts_valid_credentials(client: TestClient, monkeypatch) -> None:
    _use_settings(
        monkeypatch,
        DASHBOARD_AUTH_ENABLED=True,
        DASHBOARD_USERNAME="admin",
        DASHBOARD_PASSWORD="secret",
    )
    resp = client.get("/dashboard", headers=_basic_header("admin", "secret"))
    assert resp.status_code == 200
    assert "DoAide Trade" in resp.text


def test_dashboard_fails_closed_without_password(client: TestClient, monkeypatch) -> None:
    """Auth enabled but no password configured -> 500, never open access."""
    _use_settings(monkeypatch, DASHBOARD_AUTH_ENABLED=True, DASHBOARD_PASSWORD="")
    resp = client.get("/dashboard", headers=_basic_header("admin", "anything"))
    assert resp.status_code == 500


def test_dashboard_auth_can_be_disabled(client: TestClient, monkeypatch) -> None:
    _use_settings(monkeypatch, DASHBOARD_AUTH_ENABLED=False, DASHBOARD_PASSWORD="")
    resp = client.get("/dashboard")
    assert resp.status_code == 200
