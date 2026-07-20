"""
Tests for auth-gated API docs (audit item B1).

FastAPI's ``/docs``, ``/redoc`` and ``/openapi.json`` are reachable only with
valid dashboard credentials now — the built-in unauthenticated routes are
disabled and re-served behind ``require_auth``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

_DOC_PATHS = ["/docs", "/redoc", "/openapi.json"]


def _client(monkeypatch, tmp_path, auth):
    data_dir = tmp_path / "data_store"
    data_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("DASHBOARD_AUTH_ENABLED", "True" if auth else "False")
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "adminpw")

    from config.settings import get_settings

    get_settings.cache_clear()
    import dashboard.app as dash

    return TestClient(dash.app, raise_server_exceptions=False)


def test_docs_open_when_auth_disabled(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, auth=False)
    for path in _DOC_PATHS:
        assert client.get(path).status_code == 200, path
    # The schema is real OpenAPI and includes the money-path endpoint.
    schema = client.get("/openapi.json").json()
    assert schema["openapi"].startswith("3.")
    assert "/api/manual-trade" in schema["paths"]


def test_docs_require_auth_when_enabled(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, auth=True)
    for path in _DOC_PATHS:
        assert client.get(path).status_code == 401, path
    # With credentials the docs come back.
    for path in _DOC_PATHS:
        assert client.get(path, auth=("admin", "adminpw")).status_code == 200, path


def test_manual_trade_documented_in_schema(monkeypatch, tmp_path):
    """B6 side-effect: the request model shows up as a component schema."""
    client = _client(monkeypatch, tmp_path, auth=False)
    schema = client.get("/openapi.json").json()
    post = schema["paths"]["/api/manual-trade"]["post"]
    ref = post["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    name = ref.rsplit("/", 1)[-1]
    props = schema["components"]["schemas"][name]["properties"]
    assert {"symbol", "quantity", "entry_price"} <= set(props)
