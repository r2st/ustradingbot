"""
Tests for the dashboard rate limiter and login lockout (audit item B2).

Covers:
* the per-IP request cap on the money path (``/api/manual-trade``) → 429,
* the failed-login lockout on the HTTP Basic auth guard → 429,
* the ``RATE_LIMIT_ENABLED`` off-switch (default under the test-suite) so the
  rest of the suite is never throttled.

Each test builds its own hermetic Settings singleton with the limits it needs;
the autouse ``_reset_rate_limits`` fixture in conftest guarantees a clean slate.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _client(monkeypatch, tmp_path, **env):
    data_dir = tmp_path / "data_store"
    data_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "adminpw")
    monkeypatch.setenv("TOTAL_CAPITAL", "10000")
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))

    from config.settings import get_settings

    get_settings.cache_clear()
    from dashboard import rate_limit

    rate_limit.reset()
    import dashboard.app as dash

    return TestClient(dash.app, raise_server_exceptions=False)


_TRADE = {
    "symbol": "AAPL",
    "quantity": 10,
    "entry_price": 100.0,
    "stop_price": 95.0,
    "target_price": 110.0,
    "admin_password": "wrong",  # wrong on purpose: 403 until the limiter trips
}


def test_manual_trade_is_rate_limited(monkeypatch, tmp_path):
    client = _client(
        monkeypatch,
        tmp_path,
        DASHBOARD_AUTH_ENABLED=False,
        RATE_LIMIT_ENABLED=True,
        RATE_LIMIT_TRADE_PER_MIN=3,
    )
    # The first 3 requests are admitted (each 403 on the wrong admin password);
    # the 4th trips the per-IP limiter with a 429 + Retry-After.
    statuses = [client.post("/api/manual-trade", json=_TRADE).status_code for _ in range(4)]
    assert statuses[:3] == [403, 403, 403]
    resp = client.post("/api/manual-trade", json=_TRADE)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


def test_rate_limit_off_by_default(monkeypatch, tmp_path):
    # conftest sets RATE_LIMIT_ENABLED=False; many posts must never 429.
    client = _client(monkeypatch, tmp_path, DASHBOARD_AUTH_ENABLED=False)
    statuses = {client.post("/api/manual-trade", json=_TRADE).status_code for _ in range(10)}
    assert statuses == {403}


def test_login_lockout_after_repeated_failures(monkeypatch, tmp_path):
    client = _client(
        monkeypatch,
        tmp_path,
        DASHBOARD_AUTH_ENABLED=True,
        RATE_LIMIT_ENABLED=True,
        RATE_LIMIT_LOGIN_MAX_FAILURES=3,
        RATE_LIMIT_LOGIN_LOCKOUT_MINUTES=15,
    )
    bad = ("admin", "nope")
    # 3 failed logins are answered 401; the 4th is locked out with a 429.
    for _ in range(3):
        assert client.get("/", auth=bad).status_code == 401
    locked = client.get("/", auth=bad)
    assert locked.status_code == 429
    assert "Retry-After" in locked.headers
    # Even correct credentials are refused while the lockout is active.
    assert client.get("/", auth=("admin", "adminpw")).status_code == 429


def test_anonymous_requests_never_lock_out(monkeypatch, tmp_path):
    """A request with *no* credentials is not a guess and must not spend budget.

    A browser whose session cookie expired fires a burst of anonymous polls;
    counting those locked the legitimate operator out of their own dashboard
    within a single page load.
    """
    client = _client(
        monkeypatch,
        tmp_path,
        DASHBOARD_AUTH_ENABLED=True,
        RATE_LIMIT_ENABLED=True,
        RATE_LIMIT_LOGIN_MAX_FAILURES=3,
    )
    for _ in range(10):
        assert client.get("/", follow_redirects=False).status_code == 401
    # Correct credentials still work — no lockout was ever armed.
    assert client.get("/", auth=("admin", "adminpw")).status_code == 200


def test_lockout_is_not_extended_by_further_attempts(monkeypatch, tmp_path):
    """Retrying while locked must not push the unlock time further out."""
    client = _client(
        monkeypatch,
        tmp_path,
        DASHBOARD_AUTH_ENABLED=True,
        RATE_LIMIT_ENABLED=True,
        RATE_LIMIT_LOGIN_MAX_FAILURES=2,
        RATE_LIMIT_LOGIN_LOCKOUT_SECONDS=60,
    )
    bad = ("admin", "nope")
    for _ in range(2):
        client.get("/", auth=bad)
    first = int(client.get("/", auth=bad).headers["Retry-After"])
    # Hammering the endpoint leaves the original deadline intact (it only ever
    # counts down); before the fix each attempt re-armed a full lockout.
    for _ in range(5):
        again = int(client.get("/", auth=bad).headers["Retry-After"])
        assert again <= first


def test_lockout_escalates_on_repeat_offences(monkeypatch, tmp_path):
    """The first lockout is short; consecutive ones double up to the ceiling."""
    from dashboard.rate_limit import _lockout_seconds

    base, cap = 60.0, 900.0
    assert _lockout_seconds(1, base, cap) == 60
    assert _lockout_seconds(2, base, cap) == 120
    assert _lockout_seconds(3, base, cap) == 240
    # …and never past the configured ceiling.
    assert _lockout_seconds(10, base, cap) == cap


def test_first_lockout_uses_the_short_default(monkeypatch, tmp_path):
    client = _client(
        monkeypatch,
        tmp_path,
        DASHBOARD_AUTH_ENABLED=True,
        RATE_LIMIT_ENABLED=True,
        RATE_LIMIT_LOGIN_MAX_FAILURES=2,
    )
    bad = ("admin", "nope")
    for _ in range(2):
        client.get("/", auth=bad)
    locked = client.get("/", auth=bad)
    assert locked.status_code == 429
    # 60s default, not the 15-minute ceiling.
    assert int(locked.headers["Retry-After"]) <= 61


def test_successful_login_clears_failures(monkeypatch, tmp_path):
    client = _client(
        monkeypatch,
        tmp_path,
        DASHBOARD_AUTH_ENABLED=True,
        RATE_LIMIT_ENABLED=True,
        RATE_LIMIT_LOGIN_MAX_FAILURES=3,
    )
    good = ("admin", "adminpw")
    bad = ("admin", "nope")
    assert client.get("/", auth=bad).status_code == 401
    assert client.get("/", auth=bad).status_code == 401
    # A success resets the counter, so the budget starts over.
    assert client.get("/", auth=good).status_code == 200
    for _ in range(2):
        assert client.get("/", auth=bad).status_code == 401  # not locked yet
