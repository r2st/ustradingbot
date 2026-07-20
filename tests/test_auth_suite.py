"""
Dedicated authentication & authorization test suite (audit item T3).

Two invariants are enforced here across the *whole* application:

1. **Every mutating endpoint is closed by default.**  With
   ``DASHBOARD_AUTH_ENABLED`` on, no ``POST``/``PUT``/``DELETE`` route ever
   returns a 2xx to an unauthenticated caller.  The HTTP-Basic-gated routes
   answer with a clean ``401`` challenge; the token-gated multi-user routes
   answer ``404`` (feature disabled) — never success.

2. **Money-moving actions need the admin password**, which is now separate from
   the dashboard login password (audit item B3) with a documented fallback.

The route sweep is derived from the live app so a newly-added unguarded mutating
endpoint fails this test instead of shipping.
"""

from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import dashboard.app as dash
from config.settings import Settings

BASIC = ("admin", "s3cret")

# Multi-user routes authenticate with an ``X-User-Token``, not HTTP Basic, so
# they are exempt from the strict "unauthenticated ⇒ 401" rule (they answer 404
# when multi-user is disabled).  They are still required to never return 2xx.
_TOKEN_AUTH_PATHS = {
    "/api/users/register",
    "/api/users/login",
    "/api/users/logout",
    "/api/users/me",
    "/api/users/me/profile",
}


def _patch_settings(monkeypatch, settings: Settings) -> None:
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    for mod in (
        "dashboard.notes_router",
        "dashboard.positions_router",
        "dashboard.users_router",
        "dashboard.memory_router",
        "dashboard.insights_router",
        "dashboard.manual_trade_router",
    ):
        monkeypatch.setattr(f"{mod}.get_settings", lambda: settings, raising=False)


@pytest.fixture
def client() -> TestClient:
    return TestClient(dash.app, raise_server_exceptions=False)


@pytest.fixture
def auth_settings(tmp_path):
    d = tmp_path / "data_store"
    d.mkdir()
    return Settings(
        DASHBOARD_AUTH_ENABLED=True,
        DASHBOARD_USERNAME="admin",
        DASHBOARD_PASSWORD="s3cret",
        DASHBOARD_ADMIN_PASSWORD="adm1n",
        DATA_DIR=d,
        MULTI_USER_ENABLED=False,
        RATE_LIMIT_ENABLED=False,
    )


@pytest.fixture
def auth_env(monkeypatch, auth_settings) -> Settings:
    _patch_settings(monkeypatch, auth_settings)
    return auth_settings


def _mutating_routes():
    """(method, concrete_path) for every mutating route on the live app."""
    out = []
    for route in dash.app.routes:
        if not isinstance(route, APIRoute):
            continue
        methods = (route.methods or set()) & {"POST", "PUT", "DELETE"}
        if not methods:
            continue
        # Substitute a dummy value for any path parameter.
        path = re.sub(r"\{[^}]+\}", "x", route.path)
        for method in sorted(methods):
            out.append((method, path))
    return out


# ---------------------------------------------------------------------------
# 1. Public endpoints stay open
# ---------------------------------------------------------------------------

def test_health_is_public(client, auth_env):
    assert client.get("/health").status_code == 200


def test_mode_is_public(client, auth_env):
    assert client.get("/api/mode").status_code == 200


# ---------------------------------------------------------------------------
# 2. Every mutating endpoint is closed to anonymous callers
# ---------------------------------------------------------------------------

def test_every_mutating_endpoint_rejects_anonymous(client, auth_env):
    routes = _mutating_routes()
    assert routes, "route sweep found no mutating endpoints — sweep is broken"
    failures = []
    for method, path in routes:
        resp = client.request(method, path, json={})
        if resp.status_code // 100 == 2:
            failures.append(f"{method} {path} -> {resp.status_code} (should not be 2xx)")
            continue
        if path not in _TOKEN_AUTH_PATHS and resp.status_code != 401:
            failures.append(f"{method} {path} -> {resp.status_code} (expected 401)")
    assert not failures, "Unguarded mutating endpoints:\n" + "\n".join(failures)


def test_mutating_endpoint_allows_authenticated(client, auth_env):
    # A representative gated mutating endpoint stops returning 401 once valid
    # HTTP Basic credentials are supplied (it then fails the *admin* gate, 403).
    r = client.post("/api/positions/stop", json={"symbol": "AAPL"}, auth=BASIC)
    assert r.status_code != 401


# ---------------------------------------------------------------------------
# 3. Protected read endpoints require auth; bad creds are rejected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/api/analytics/summary",
    "/api/risk/report",
    "/api/history/stats",
    "/api/memory/overview",
    "/api/alerts/rules",
    "/api/notes",
])
def test_protected_get_requires_auth(client, auth_env, path):
    assert client.get(path).status_code == 401
    assert client.get(path, auth=BASIC).status_code == 200


def test_wrong_password_rejected(client, auth_env):
    r = client.get("/api/analytics/summary", auth=("admin", "WRONG"))
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Basic"


def test_wrong_username_rejected(client, auth_env):
    assert client.get("/api/analytics/summary", auth=("root", "s3cret")).status_code == 401


def test_misconfigured_no_password_fails_closed(client, monkeypatch, tmp_path):
    d = tmp_path / "ds"
    d.mkdir()
    settings = Settings(DASHBOARD_AUTH_ENABLED=True, DASHBOARD_PASSWORD="",
                        DATA_DIR=d, RATE_LIMIT_ENABLED=False)
    _patch_settings(monkeypatch, settings)
    # Auth enabled but no password configured → 500 fail-closed, never 200.
    assert client.get("/api/analytics/summary", auth=BASIC).status_code == 500


# ---------------------------------------------------------------------------
# 4. Admin-password gate on money-moving actions
# ---------------------------------------------------------------------------

def test_manual_trade_wrong_admin_password_403(client, auth_env):
    # A fully valid body (so request validation passes) with the wrong admin
    # password is rejected at the admin gate — 403, not 422.
    r = client.post(
        "/api/manual-trade",
        json={"symbol": "AAPL", "quantity": 10, "entry_price": 100.0,
              "admin_password": "nope"},
        auth=BASIC,
    )
    assert r.status_code == 403


def test_manual_trade_correct_admin_password_passes_gate(client, auth_env, monkeypatch):
    class _Result:
        def to_dict(self):
            return {"ok": True}

    monkeypatch.setattr(
        "execution.manual_trade.place_manual_trade", lambda body, settings: _Result()
    )
    r = client.post(
        "/api/manual-trade",
        json={"symbol": "AAPL", "quantity": 10, "entry_price": 100.0,
              "admin_password": "adm1n"},
        auth=BASIC,
    )
    assert r.status_code == 200 and r.json()["ok"] is True


def test_position_stop_wrong_admin_password_403(client, auth_env):
    r = client.post(
        "/api/positions/stop",
        json={"symbol": "AAPL", "admin_password": "nope"},
        auth=BASIC,
    )
    assert r.status_code == 403


def test_engine_control_bad_admin_password_reports_invalid(client, auth_env):
    # Engine control returns a 200 envelope with ok=False for a bad password
    # (it degrades rather than raising), so assert on the payload, not status.
    r = client.post(
        "/api/engine/control",
        json={"action": "restart", "admin_password": "nope"},
        auth=BASIC,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and "password" in body["message"].lower()


# ---------------------------------------------------------------------------
# 5. B3 — separate admin password with DASHBOARD_PASSWORD fallback
# ---------------------------------------------------------------------------

def test_admin_password_falls_back_to_dashboard_password(client, monkeypatch, tmp_path):
    # No dedicated admin password → the dashboard password gates admin actions.
    d = tmp_path / "ds"
    d.mkdir()
    settings = Settings(
        DASHBOARD_AUTH_ENABLED=True, DASHBOARD_USERNAME="admin",
        DASHBOARD_PASSWORD="s3cret", DASHBOARD_ADMIN_PASSWORD="",
        DATA_DIR=d, RATE_LIMIT_ENABLED=False,
    )
    _patch_settings(monkeypatch, settings)
    monkeypatch.setattr(
        "execution.stop_trade.stop_open_position",
        lambda symbol, s: type("R", (), {"to_dict": lambda self: {"ok": True}})(),
    )
    r = client.post(
        "/api/positions/stop",
        json={"symbol": "AAPL", "admin_password": "s3cret"},
        auth=BASIC,
    )
    assert r.status_code == 200  # login password accepted as admin (fallback)


def test_dedicated_admin_password_supersedes_login_password(client, auth_env):
    # DASHBOARD_ADMIN_PASSWORD is set (adm1n) and differs from the login
    # password (s3cret); the login password must NOT unlock admin actions.
    r = client.post(
        "/api/positions/stop",
        json={"symbol": "AAPL", "admin_password": "s3cret"},
        auth=BASIC,
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 6. B10 — failed logins are logged at WARN with client context
# ---------------------------------------------------------------------------

def test_failed_login_is_logged(client, auth_env):
    import structlog

    with structlog.testing.capture_logs() as logs:
        client.get("/api/analytics/summary", auth=("admin", "WRONG"))
    failures = [e for e in logs if e.get("event") == "auth.failure"]
    assert failures, "expected an auth.failure log event"
    rec = failures[-1]
    assert rec["log_level"] == "warning"
    assert rec["reason"] == "invalid_credentials"
    assert "ip" in rec and "timestamp" in rec


def test_missing_credentials_is_logged(client, auth_env):
    import structlog

    with structlog.testing.capture_logs() as logs:
        client.get("/api/analytics/summary")
    reasons = {e.get("reason") for e in logs if e.get("event") == "auth.failure"}
    assert "missing_credentials" in reasons
