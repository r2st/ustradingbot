"""Static assets carry a Cache-Control header so pages stop revalidating them."""

from fastapi.testclient import TestClient

from dashboard.app import CachedStaticFiles, app

client = TestClient(app)


def test_static_asset_is_cacheable():
    r = client.get("/static/viral.css")
    assert r.status_code == 200
    assert r.headers["cache-control"] == CachedStaticFiles.CACHE_CONTROL
    assert "max-age=86400" in r.headers["cache-control"]


def test_missing_static_asset_is_not_cached():
    r = client.get("/static/does-not-exist.js")
    assert r.status_code == 404
    assert "max-age" not in r.headers.get("cache-control", "")
