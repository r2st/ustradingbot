"""
Tests for the branded sign-in page and its session cookie.

The dashboard authenticated only with HTTP Basic, so the first thing a user saw
was the browser's native credential dialog.  A ``/login`` page plus a signed
session cookie now sits alongside Basic auth — Basic still works everywhere, so
these tests pin down the *new* surface and, just as importantly, that it did not
become a weaker way in:

* the token is signed, expiring and tamper-evident (:mod:`dashboard.session`);
* a browser navigation gets the login page, an API client still gets the Basic
  challenge (that distinction is what keeps the native dialog away);
* wrong credentials are rejected and never mint a cookie.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
from config.settings import Settings
from dashboard.session import COOKIE_NAME, issue_token, verify_token

PASSWORD = "s3cret"
USERNAME = "admin"


@pytest.fixture
def client() -> TestClient:
    # follow_redirects=False so the 303s are asserted directly.
    return TestClient(dash.app, raise_server_exceptions=False, follow_redirects=False)


@pytest.fixture
def auth_env(monkeypatch, tmp_path):
    """Auth on, rate limiting off (the lockout would mask assertions)."""
    data_dir = tmp_path / "ds"
    data_dir.mkdir()
    settings = Settings(
        DASHBOARD_AUTH_ENABLED=True,
        DASHBOARD_USERNAME=USERNAME,
        DASHBOARD_PASSWORD=PASSWORD,
        DATA_DIR=data_dir,
        RATE_LIMIT_ENABLED=False,
    )
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    return settings


# --------------------------------------------------------------------------- #
# token primitives
# --------------------------------------------------------------------------- #

def test_token_round_trip():
    token = issue_token(USERNAME, PASSWORD)
    assert verify_token(token, PASSWORD) == USERNAME


def test_token_rejected_under_wrong_password():
    """Rotating the dashboard password must invalidate outstanding sessions."""
    token = issue_token(USERNAME, PASSWORD)
    assert verify_token(token, "a-different-password") is None


def test_token_rejects_tampered_payload():
    """Editing the payload (e.g. to extend the expiry) must break the signature."""
    token = issue_token(USERNAME, PASSWORD)
    body, _, sig = token.partition(".")
    forged = f"{body}x.{sig}"
    assert verify_token(forged, PASSWORD) is None


def test_token_rejects_tampered_signature():
    token = issue_token(USERNAME, PASSWORD)
    body, _, _ = token.partition(".")
    assert verify_token(f"{body}.deadbeef", PASSWORD) is None


def test_expired_token_rejected():
    token = issue_token(USERNAME, PASSWORD, ttl_seconds=-1)
    assert verify_token(token, PASSWORD) is None


def test_token_expiry_is_in_the_future():
    before = int(time.time())
    token = issue_token(USERNAME, PASSWORD, ttl_seconds=60)
    assert verify_token(token, PASSWORD) == USERNAME
    # Still valid a moment later, i.e. the expiry really was absolute.
    assert int(time.time()) - before < 60


@pytest.mark.parametrize("bad", ["", "nodot", ".", "a.b", "....", "x" * 200])
def test_malformed_tokens_rejected(bad):
    assert verify_token(bad, PASSWORD) is None


def test_no_password_means_no_sessions():
    """With no password configured nothing can be signed or verified."""
    assert issue_token(USERNAME, "") == ""
    assert verify_token("anything.here", "") is None


# --------------------------------------------------------------------------- #
# login page + form
# --------------------------------------------------------------------------- #

def test_login_page_redirects_to_landing(client, auth_env):
    resp = client.get("/login")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_landing_page_is_public(client, auth_env):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Sign in" in resp.text


def test_browser_navigation_is_sent_to_landing(client, auth_env):
    """An unauthenticated *browser* request bounces to the landing page…"""
    resp = client.get("/dashboard", headers={"Accept": "text/html"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_api_client_still_gets_basic_challenge(client, auth_env):
    """…while a non-browser client keeps the standard Basic challenge.

    This split is the whole point: it is the ``WWW-Authenticate`` header on an
    HTML navigation that makes the browser show its native credential dialog.
    """
    resp = client.get("/dashboard")
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Basic"


def test_successful_login_sets_session_cookie(client, auth_env):
    resp = client.post(
        "/login",
        data={"username": USERNAME, "password": PASSWORD},
        headers={"Accept": "text/html"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    cookie = resp.cookies.get(COOKIE_NAME)
    assert cookie
    assert verify_token(cookie, PASSWORD) == USERNAME
    # Hardening flags travel with the cookie.
    raw = resp.headers["set-cookie"].lower()
    assert "httponly" in raw
    assert "samesite=strict" in raw


def test_session_cookie_grants_access(client, auth_env):
    token = issue_token(USERNAME, PASSWORD)
    resp = client.get("/dashboard", headers={"Accept": "text/html"}, cookies={COOKIE_NAME: token})
    assert resp.status_code == 200


def test_forged_cookie_does_not_grant_access(client, auth_env):
    resp = client.get(
        "/dashboard",
        headers={"Accept": "text/html"},
        cookies={COOKIE_NAME: "YWRtaW58OTk5OTk5OTk5OQ.notarealsignature"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_expired_cookie_does_not_grant_access(client, auth_env):
    stale = issue_token(USERNAME, PASSWORD, ttl_seconds=-1)
    resp = client.get("/dashboard", headers={"Accept": "text/html"}, cookies={COOKIE_NAME: stale})
    assert resp.status_code == 303


def test_wrong_password_rejected_without_cookie(client, auth_env):
    resp = client.post(
        "/login",
        data={"username": USERNAME, "password": "WRONG"},
        headers={"Accept": "text/html"},
    )
    assert resp.status_code == 303
    assert COOKIE_NAME not in resp.cookies
    assert "error=Incorrect" in resp.headers["location"]


def test_wrong_username_rejected(client, auth_env):
    resp = client.post(
        "/login",
        data={"username": "root", "password": PASSWORD},
        headers={"Accept": "text/html"},
    )
    assert resp.status_code == 303
    assert COOKIE_NAME not in resp.cookies


def test_empty_credentials_rejected_with_401_not_422(client, auth_env):
    """The anonymous-sweep in test_auth_suite requires 401, not a validation error."""
    resp = client.post("/login", json={})
    assert resp.status_code == 401


def test_json_login_supported(client, auth_env):
    resp = client.post("/login", json={"username": USERNAME, "password": PASSWORD})
    assert resp.status_code == 303
    assert verify_token(resp.cookies.get(COOKIE_NAME), PASSWORD) == USERNAME


def test_logout_requires_auth(client, auth_env):
    assert client.post("/logout").status_code == 401


def test_logout_clears_cookie(client, auth_env):
    token = issue_token(USERNAME, PASSWORD)
    resp = client.post("/logout", cookies={COOKIE_NAME: token})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    # An expiry in the past is how a cookie is deleted.
    assert COOKIE_NAME in resp.headers.get("set-cookie", "")


def test_login_page_redirects_when_already_signed_in(client, auth_env):
    token = issue_token(USERNAME, PASSWORD)
    resp = client.get("/login", cookies={COOKIE_NAME: token})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


# --------------------------------------------------------------------------- #
# branding
# --------------------------------------------------------------------------- #

def test_landing_page_has_doaide_branding(client, auth_env):
    """The landing page shows the DoAide Trade brand, not the old USTradingBot one."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "DoAide" in resp.text
    assert "Trade" in resp.text
    assert "USTradingBot" not in resp.text
    assert "Bull Circuit" not in resp.text


def test_landing_page_uses_doaide_theme_colors(client, auth_env):
    """Core DoAide palette tokens are present in the landing page CSS."""
    resp = client.get("/")
    assert "#0A0A0B" in resp.text
    assert "#F0B429" in resp.text


def test_landing_page_loads_doaide_fonts(client, auth_env):
    """The three DoAide typefaces are loaded from Google Fonts."""
    resp = client.get("/")
    assert "Instrument+Serif" in resp.text or "Instrument Serif" in resp.text
    assert "Schibsted+Grotesk" in resp.text or "Schibsted Grotesk" in resp.text
    assert "IBM+Plex+Mono" in resp.text or "IBM Plex Mono" in resp.text
