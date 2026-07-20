"""
Tests for AI/LLM-endpoint rate limiting (audit item B-1).

The commentary poll, market-overview poll, and the forced refresh all drive
paid OpenRouter calls, so each carries a per-IP ``RATE_LIMIT_AI_PER_MIN`` cap
distinct from the money/control caps.  These tests assert the cap trips with a
429 + ``Retry-After`` and that it is off when limiting is disabled.
"""

from __future__ import annotations

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


def test_ai_commentary_is_rate_limited(monkeypatch, tmp_path):
    client = _client(
        monkeypatch,
        tmp_path,
        DASHBOARD_AUTH_ENABLED=False,
        RATE_LIMIT_ENABLED=True,
        RATE_LIMIT_AI_PER_MIN=3,
    )
    statuses = [client.get("/api/ai/commentary").status_code for _ in range(3)]
    assert statuses == [200, 200, 200]
    resp = client.get("/api/ai/commentary")
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


def test_ai_refresh_is_rate_limited(monkeypatch, tmp_path):
    client = _client(
        monkeypatch,
        tmp_path,
        DASHBOARD_AUTH_ENABLED=False,
        RATE_LIMIT_ENABLED=True,
        RATE_LIMIT_AI_PER_MIN=2,
    )
    # /refresh may answer 200 (started) or 429 (budget exhausted); either way,
    # once the *rate* cap is spent every further call is a 429.
    first = [client.post("/api/ai/refresh").status_code for _ in range(2)]
    assert all(s in (200, 429) for s in first)
    limited = client.post("/api/ai/refresh")
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers


def test_ai_market_overview_is_rate_limited(monkeypatch, tmp_path):
    client = _client(
        monkeypatch,
        tmp_path,
        DASHBOARD_AUTH_ENABLED=False,
        RATE_LIMIT_ENABLED=True,
        RATE_LIMIT_AI_PER_MIN=2,
    )
    assert client.get("/api/ai/market-overview").status_code == 200
    assert client.get("/api/ai/market-overview").status_code == 200
    assert client.get("/api/ai/market-overview").status_code == 429


def test_ai_endpoints_not_limited_when_disabled(monkeypatch, tmp_path):
    # conftest sets RATE_LIMIT_ENABLED=False; heavy polling must never 429.
    client = _client(monkeypatch, tmp_path, DASHBOARD_AUTH_ENABLED=False)
    statuses = {client.get("/api/ai/commentary").status_code for _ in range(15)}
    assert statuses == {200}
