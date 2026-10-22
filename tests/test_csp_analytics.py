"""The CSP must allow the Umami script every template loads."""

from __future__ import annotations

from fastapi.testclient import TestClient

from dashboard.app import app

client = TestClient(app, raise_server_exceptions=False)


def _directive(csp: str, name: str) -> str:
    return next(d for d in csp.split(";") if d.strip().startswith(name))


def test_csp_allows_umami_but_stays_locked_down():
    r = client.get("/tools")
    assert "https://analytics.doaide.com/script.js" in r.text
    csp = r.headers["content-security-policy"]
    assert "https://analytics.doaide.com" in _directive(csp, "script-src")
    assert "https://analytics.doaide.com" in _directive(csp, "connect-src")
    assert "default-src 'self'" in csp
    assert "*" not in _directive(csp, "script-src")
