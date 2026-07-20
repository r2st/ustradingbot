"""
Tests for the shared HTTP plumbing added in the audit (B5 / B7 / B13):

* body-size limit rejects >1 MiB with 413,
* malformed JSON bodies return 422 (not 500),
* every error response carries the standardized ``{ok, error, detail}`` shape.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from dashboard.http_util import (
    MAX_BODY_BYTES,
    BodySizeLimitMiddleware,
    error_body,
    ok,
    parse_json_body,
)
from dashboard.middleware import install_exception_handlers


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=1024)  # tiny for tests
    install_exception_handlers(app)

    @app.post("/echo")
    async def echo(request: Request):
        body = await parse_json_body(request)
        return {"got": body}

    @app.post("/boom")
    async def boom(request: Request):
        raise RuntimeError("kaboom")

    return app


@pytest.fixture()
def client() -> TestClient:
    # raise_server_exceptions=False so the 500 handler's response is observed
    # instead of the exception propagating into the test.
    return TestClient(_make_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# B5 — body size limit
# ---------------------------------------------------------------------------


def test_body_under_limit_ok(client: TestClient):
    r = client.post("/echo", json={"a": 1})
    assert r.status_code == 200
    assert r.json()["got"] == {"a": 1}


def test_body_over_limit_413_content_length(client: TestClient):
    payload = json.dumps({"x": "a" * 5000})
    r = client.post("/echo", data=payload,
                    headers={"content-type": "application/json"})
    assert r.status_code == 413
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "payload_too_large"


def test_body_over_limit_streaming_413(client: TestClient):
    # No Content-Length: a generator body forces chunked transfer.
    def gen():
        for _ in range(20):
            yield b"a" * 200

    r = client.post("/echo", content=gen(),
                    headers={"content-type": "application/octet-stream"})
    assert r.status_code == 413


# ---------------------------------------------------------------------------
# B7 — malformed JSON → 422
# ---------------------------------------------------------------------------


def test_malformed_json_returns_422(client: TestClient):
    r = client.post("/echo", data="{not json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert r.json()["ok"] is False


def test_empty_body_is_empty_dict(client: TestClient):
    r = client.post("/echo")
    assert r.status_code == 200
    assert r.json()["got"] == {}


def test_non_object_json_returns_422(client: TestClient):
    r = client.post("/echo", data="[1,2,3]",
                    headers={"content-type": "application/json"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# B13 — consistent error + success envelopes
# ---------------------------------------------------------------------------


def test_unhandled_error_standard_shape(client: TestClient):
    r = client.post("/boom", json={})
    assert r.status_code == 500
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["status"] == 500
    assert body["error"]["code"] == "internal_error"
    # Legacy aliases preserved.
    assert "detail" in body and body["error_code"] == "internal_error"


def test_error_body_helper_shape():
    b = error_body(404, "nope", code="not_found")
    assert b["ok"] is False
    assert b["error"] == {"code": "not_found", "message": "nope", "status": 404}
    assert b["detail"] == "nope"


def test_ok_envelope_helper():
    assert ok({"n": 1}) == {"ok": True, "data": {"n": 1}}
    assert ok() == {"ok": True}
    assert ok(count=3) == {"ok": True, "count": 3}


def test_limit_constant_is_one_mib():
    assert MAX_BODY_BYTES == 1024 * 1024
