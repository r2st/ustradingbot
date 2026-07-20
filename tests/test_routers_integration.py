"""
TestClient integration tests for the feature routers (audit item T2).

Each router is exercised through the real FastAPI app for three cases:

* **happy path** — a well-formed request returns 200 and the expected shape;
* **auth required** — with ``DASHBOARD_AUTH_ENABLED`` on, an unauthenticated
  request is rejected with 401;
* **bad input** — a malformed request is rejected with a 4xx (not a 500).

External / network-bound collaborators (the AI commentary engine, the broker
close path, the market-regime fetch) are patched so the tests are hermetic and
fast — the point here is the routing + auth + response shaping, not the
analytics behind them (those have their own unit tests).

Covers: ai_router, alerts_router, notes_router, positions_router, users_router,
memory_router, rationale_router, insights_router.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
from config.settings import Settings


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

# The router modules that bind their own ``get_settings`` reference at import
# time (``from config.settings import get_settings``) — each must be patched
# for the settings override to take effect.  Routers that go through
# ``dashboard.auth.get_settings`` (alerts, rationale, ai) are covered by
# patching ``dashboard.app.get_settings`` alone.
_DIRECT_GET_SETTINGS_MODULES = (
    "dashboard.notes_router",
    "dashboard.positions_router",
    "dashboard.users_router",
    "dashboard.memory_router",
    "dashboard.insights_router",
)


def _patch_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """Point every relevant ``get_settings`` binding at *settings*."""
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    for mod in _DIRECT_GET_SETTINGS_MODULES:
        monkeypatch.setattr(f"{mod}.get_settings", lambda: settings, raising=False)


@pytest.fixture
def client() -> TestClient:
    return TestClient(dash.app, raise_server_exceptions=False)


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data_store"
    d.mkdir()
    return d


@pytest.fixture
def open_env(monkeypatch, data_dir) -> Settings:
    """Auth-disabled settings wired into every router (happy-path / bad-input)."""
    settings = Settings(
        DASHBOARD_AUTH_ENABLED=False,
        DASHBOARD_ADMIN_PASSWORD="adm1n",
        DATA_DIR=data_dir,
        MULTI_USER_ENABLED=True,
        RATE_LIMIT_ENABLED=False,
    )
    _patch_settings(monkeypatch, settings)
    return settings


@pytest.fixture
def auth_env(monkeypatch, data_dir) -> Settings:
    """Auth-enabled settings so the 401 path can be exercised."""
    settings = Settings(
        DASHBOARD_AUTH_ENABLED=True,
        DASHBOARD_USERNAME="admin",
        DASHBOARD_PASSWORD="s3cret",
        DASHBOARD_ADMIN_PASSWORD="adm1n",
        DATA_DIR=data_dir,
        MULTI_USER_ENABLED=True,
        RATE_LIMIT_ENABLED=False,
    )
    _patch_settings(monkeypatch, settings)
    return settings


# ===========================================================================
# ai_router
# ===========================================================================

class _FakeEngine:
    """Deterministic stand-in for the AI commentary engine (no network)."""

    def __init__(self, budget_out: bool = False) -> None:
        self._budget_out = budget_out

    def note_poll(self) -> None:  # pragma: no cover - trivial
        pass

    def should_refresh(self) -> bool:
        return False

    def budget_exhausted(self) -> bool:
        return self._budget_out

    def status(self) -> Dict[str, Any]:
        return {"last_run": None, "last_error": None, "model": "test"}

    def payload_for_client(self) -> Dict[str, Any]:
        return {
            "generated_at": None,
            "positions": [],
            "watchlist": [],
            "market": {"regime": "bull"},
            "stale": True,
            "market_open": False,
        }


def test_ai_commentary_happy(client, open_env, monkeypatch):
    monkeypatch.setattr("dashboard.ai_router._engine", lambda: _FakeEngine())
    r = client.get("/api/ai/commentary")
    assert r.status_code == 200
    body = r.json()
    assert "market" in body and body["market"]["regime"] == "bull"


def test_ai_market_overview_happy(client, open_env, monkeypatch):
    monkeypatch.setattr("dashboard.ai_router._engine", lambda: _FakeEngine())
    r = client.get("/api/ai/market-overview")
    assert r.status_code == 200
    assert r.json()["market"]["regime"] == "bull"


def test_ai_refresh_budget_exhausted_429(client, open_env, monkeypatch):
    monkeypatch.setattr(
        "dashboard.ai_router._engine", lambda: _FakeEngine(budget_out=True)
    )
    r = client.post("/api/ai/refresh")
    assert r.status_code == 429


def test_ai_refresh_ok(client, open_env, monkeypatch):
    monkeypatch.setattr("dashboard.ai_router._engine", lambda: _FakeEngine())
    monkeypatch.setattr("dashboard.ai_router._schedule_refresh", lambda *a, **k: None)
    r = client.post("/api/ai/refresh")
    assert r.status_code == 200 and r.json()["started"] is True


def test_ai_commentary_auth_required(client, auth_env):
    assert client.get("/api/ai/commentary").status_code == 401


# ===========================================================================
# alerts_router
# ===========================================================================

def test_alerts_rules_happy(client, open_env):
    r = client.get("/api/alerts/rules")
    assert r.status_code == 200
    body = r.json()
    assert "rules" in body and "event_types" in body and "channels" in body


def test_alerts_channels_happy(client, open_env):
    r = client.get("/api/alerts/channels")
    assert r.status_code == 200
    assert set(r.json()["channels"]) == {"telegram", "email", "push"}


def test_alerts_test_bad_channel_400(client, open_env):
    r = client.post("/api/alerts/test", json={"channel": "carrier-pigeon"})
    assert r.status_code == 400


def test_alerts_test_push_ok(client, open_env):
    # 'push' is file-based (no external service), so a test dispatch succeeds.
    r = client.post("/api/alerts/test", json={"channel": "push"})
    assert r.status_code == 200
    assert r.json()["channel"] == "push"


def test_alerts_rules_auth_required(client, auth_env):
    assert client.get("/api/alerts/rules").status_code == 401


# ===========================================================================
# notes_router
# ===========================================================================

def test_notes_set_get_delete_happy(client, open_env):
    r = client.post("/api/notes/T1", json={"note": "clean breakout", "tags": ["win"]})
    assert r.status_code == 200
    assert r.json()["note"] == "clean breakout"

    r = client.get("/api/notes/T1")
    assert r.status_code == 200 and r.json()["tags"] == ["win"]

    r = client.get("/api/notes?q=breakout")
    assert r.json()["count"] == 1

    assert client.delete("/api/notes/T1").json()["ok"] is True


def test_notes_bad_tags_400(client, open_env):
    r = client.post("/api/notes/T1", json={"note": "x", "tags": "not-a-list"})
    assert r.status_code == 400


def test_notes_auth_required(client, auth_env):
    assert client.get("/api/notes").status_code == 401


# ===========================================================================
# positions_router  (admin-gated)
# ===========================================================================

def test_positions_stop_requires_admin_password_403(client, open_env):
    r = client.post("/api/positions/stop", json={"symbol": "AAPL"})
    assert r.status_code == 403


def test_positions_stop_missing_symbol_422(client, open_env):
    # An empty symbol fails request-body validation (Pydantic pattern) → 422,
    # before any broker work.
    r = client.post(
        "/api/positions/stop",
        json={"symbol": "", "admin_password": open_env.admin_password},
    )
    assert r.status_code == 422


def test_positions_stop_happy(client, open_env, monkeypatch):
    class _Result:
        def to_dict(self):
            return {"ok": True, "symbol": "AAPL", "message": "closed"}

    monkeypatch.setattr(
        "execution.stop_trade.stop_open_position", lambda symbol, settings: _Result()
    )
    r = client.post(
        "/api/positions/stop",
        json={"symbol": "AAPL", "admin_password": open_env.admin_password},
    )
    assert r.status_code == 200 and r.json()["ok"] is True


def test_positions_stop_auth_required(client, auth_env):
    r = client.post("/api/positions/stop", json={"symbol": "AAPL"})
    assert r.status_code == 401


# ===========================================================================
# users_router  (multi-user)
# ===========================================================================

def test_users_register_login_me_happy(client, open_env):
    r = client.post(
        "/api/users/register", json={"username": "alice", "password": "hunter2pass"}
    )
    assert r.status_code == 200 and r.json()["username"] == "alice"

    r = client.post(
        "/api/users/login", json={"username": "alice", "password": "hunter2pass"}
    )
    assert r.status_code == 200
    token = r.json()["token"]

    r = client.get("/api/users/me", headers={"X-User-Token": token})
    assert r.status_code == 200 and r.json()["username"] == "alice"


def test_users_login_bad_credentials_401(client, open_env):
    r = client.post(
        "/api/users/login", json={"username": "nobody", "password": "whatever123"}
    )
    assert r.status_code == 401


def test_users_register_bad_input_400(client, open_env):
    r = client.post("/api/users/register", json={"username": "", "password": "x"})
    assert r.status_code == 400


def test_users_me_bad_token_401(client, open_env):
    r = client.get("/api/users/me", headers={"X-User-Token": "garbage"})
    assert r.status_code == 401


def test_users_disabled_returns_404(client, monkeypatch, data_dir):
    settings = Settings(
        DASHBOARD_AUTH_ENABLED=False, DATA_DIR=data_dir, MULTI_USER_ENABLED=False
    )
    _patch_settings(monkeypatch, settings)
    r = client.post("/api/users/login", json={"username": "a", "password": "b"})
    assert r.status_code == 404


def test_users_admin_list_auth_required(client, auth_env):
    # GET /api/users is HTTP-Basic admin-gated.
    assert client.get("/api/users").status_code == 401


# ===========================================================================
# memory_router  (read-only, fail-soft)
# ===========================================================================

def test_memory_overview_happy_empty(client, open_env):
    r = client.get("/api/memory/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["learnings"] == [] and "stats" in body


def test_memory_endpoints_happy(client, open_env):
    for path, key in (
        ("/api/memory/learnings", "learnings"),
        ("/api/memory/reflections", "reflections"),
        ("/api/memory/guard-decisions", "decisions"),
    ):
        r = client.get(path)
        assert r.status_code == 200 and key in r.json()


def test_memory_stats_happy(client, open_env):
    r = client.get("/api/memory/stats")
    assert r.status_code == 200
    assert r.json()["total_learnings"] == 0


def test_memory_auth_required(client, auth_env):
    assert client.get("/api/memory/overview").status_code == 401


# ===========================================================================
# rationale_router  (read-only)
# ===========================================================================

def test_rationale_list_happy_empty(client, open_env):
    r = client.get("/api/rationale")
    assert r.status_code == 200
    assert r.json() == {"records": [], "count": 0}


def test_rationale_by_symbol_happy(client, open_env):
    r = client.get("/api/rationale?symbol=AAPL")
    assert r.status_code == 200
    assert r.json() == {"record": None}


def test_rationale_auth_required(client, auth_env):
    assert client.get("/api/rationale").status_code == 401


# ===========================================================================
# insights_router
# ===========================================================================

def test_insights_montecarlo_happy(client, open_env):
    # Empty journal → the projection returns its empty-shape dict, not a 500.
    r = client.get("/api/montecarlo?runs=100&horizon=10")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


def test_insights_autotune_happy(client, open_env):
    r = client.get("/api/autotune")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


def test_insights_montecarlo_bad_input_422(client, open_env):
    # runs must be an int — a non-numeric query value is a validation error.
    r = client.get("/api/montecarlo?runs=not-a-number")
    assert r.status_code == 422


def test_insights_auth_required(client, auth_env):
    assert client.get("/api/montecarlo").status_code == 401
