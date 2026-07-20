"""
Tests for the same-origin CSRF guard (audit item B-5).

State-changing methods with a cross-site ``Origin``/``Referer`` are rejected
with 403; same-origin or header-less (non-browser) requests pass through; and
safe methods (GET) are never gated.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _client(monkeypatch, tmp_path, **env):
    data_dir = tmp_path / "data_store"
    data_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "False")
    monkeypatch.setenv("TOTAL_CAPITAL", "10000")
    monkeypatch.setenv("CSRF_PROTECTION_ENABLED", "True")
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))

    from config.settings import get_settings

    get_settings.cache_clear()
    import dashboard.app as dash

    return TestClient(dash.app, raise_server_exceptions=False)


_TRADE = {
    "symbol": "AAPL", "quantity": 1, "entry_price": 100.0,
    "stop_price": 95.0, "target_price": 110.0, "admin_password": "wrong",
}


def test_cross_origin_post_blocked(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    resp = client.post(
        "/api/manual-trade", json=_TRADE,
        headers={"Origin": "https://evil.example.com", "Host": "dash.local"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"


def test_same_origin_post_allowed(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    resp = client.post(
        "/api/manual-trade", json=_TRADE,
        headers={"Origin": "http://dash.local", "Host": "dash.local"},
    )
    # Passes the CSRF gate → reaches the handler → 403 on the *wrong password*
    # (not the CSRF 403).
    assert resp.status_code == 403
    assert resp.json()["error"].get("code") != "csrf_failed"


def test_no_origin_header_allowed(monkeypatch, tmp_path):
    # Non-browser client (curl / API) sends no Origin → not a CSRF attack.
    client = _client(monkeypatch, tmp_path)
    resp = client.post("/api/manual-trade", json=_TRADE)
    assert resp.status_code == 403
    assert resp.json()["error"].get("code") != "csrf_failed"


def test_referer_fallback_blocked(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    resp = client.post(
        "/api/manual-trade", json=_TRADE,
        headers={"Referer": "https://evil.example.com/x", "Host": "dash.local"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"


def test_trusted_origin_allowed(monkeypatch, tmp_path):
    client = _client(
        monkeypatch, tmp_path,
        CSRF_TRUSTED_ORIGINS="https://public.example.com",
    )
    resp = client.post(
        "/api/manual-trade", json=_TRADE,
        headers={"Origin": "https://public.example.com", "Host": "127.0.0.1:8501"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"].get("code") != "csrf_failed"


def test_get_never_gated(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    resp = client.get(
        "/api/mode", headers={"Origin": "https://evil.example.com"}
    )
    assert resp.status_code == 200


def test_x_forwarded_host_is_same_origin(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    resp = client.post(
        "/api/manual-trade", json=_TRADE,
        headers={
            "Origin": "https://pub.example.com",
            "Host": "127.0.0.1:8501",
            "X-Forwarded-Host": "pub.example.com",
        },
    )
    assert resp.status_code == 403
    assert resp.json()["error"].get("code") != "csrf_failed"
