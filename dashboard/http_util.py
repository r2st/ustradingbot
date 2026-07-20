"""
Shared HTTP plumbing for the dashboard API (audit items B5, B7, B13).

This module centralises three concerns that were previously handled
ad-hoc (or not at all) across the ~35 feature routers:

* **B5 — Request body size limit.**  :class:`BodySizeLimitMiddleware` rejects
  any request whose body exceeds :data:`MAX_BODY_BYTES` (1 MiB) with a clean
  ``413 Payload Too Large`` instead of letting a multi-megabyte upload be
  buffered into memory.  It guards both the ``Content-Length`` fast-path and
  streaming/chunked bodies (by counting bytes as they arrive).

* **B7 — Consistent bad-JSON handling.**  :func:`parse_json_body` replaces the
  bare ``await request.json()`` calls that raised an unhandled
  ``json.JSONDecodeError`` (surfacing to the client as an opaque 500).  It
  raises a ``422 Unprocessable Entity`` with a standard error body instead.

* **B13 — Consistent response shapes.**  :func:`ok` / :func:`fail` build a
  shared envelope and :func:`error_body` is the canonical error shape.  The
  exception handlers in :mod:`dashboard.middleware` render *every* error
  response — validation, HTTP, or unexpected — through it, so the whole API
  answers in the same ``{"ok": false, "error": {...}}`` shape.  The legacy
  ``detail`` / ``error_code`` keys are preserved alongside so existing clients
  keep working.
"""

from __future__ import annotations

from typing import Any, Dict

import structlog
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# B5 — request body size limit
# ---------------------------------------------------------------------------

MAX_BODY_BYTES = 1024 * 1024  # 1 MiB


def _too_large_response() -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content=error_body(
            413,
            f"Request body exceeds the {MAX_BODY_BYTES // 1024} KiB limit.",
            code="payload_too_large",
        ),
    )


class BodySizeLimitMiddleware:
    """Pure-ASGI middleware rejecting bodies larger than *max_bytes*.

    A ``Content-Length`` over the limit is refused before the body is read.
    Otherwise the body is buffered up to the cap; the moment the running total
    crosses ``max_bytes`` the request is refused with a 413 — so a chunked /
    streaming client cannot sneak past the limit either.  Buffering is bounded
    by the cap itself (1 MiB), so this cannot be used to exhaust memory.
    """

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Fast path: an honest Content-Length lets us refuse before reading.
        headers = dict(scope.get("headers") or [])
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_bytes:
                    await _too_large_response()(scope, receive, send)
                    return
            except (ValueError, TypeError):
                pass  # malformed header — fall through to the streaming guard

        # Buffer the whole body up to the cap so we can refuse before the route
        # handler runs.  Requests without a body (GET/DELETE) short-circuit on
        # the first `more_body == False` chunk.
        chunks: list[bytes] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # e.g. http.disconnect — pass it straight through.
                chunks.append(message)  # type: ignore[arg-type]
                break
            body = message.get("body", b"") or b""
            received += len(body)
            if received > self.max_bytes:
                await _too_large_response()(scope, receive, send)
                return
            chunks.append(message)  # type: ignore[arg-type]
            if not message.get("more_body", False):
                break

        replay = list(chunks)

        async def replay_receive() -> Message:
            if replay:
                return replay.pop(0)  # type: ignore[return-value]
            return await receive()

        await self.app(scope, replay_receive, send)


# ---------------------------------------------------------------------------
# B7 — consistent bad-JSON handling
# ---------------------------------------------------------------------------


async def parse_json_body(
    request: Request, *, require_object: bool = True
) -> Dict[str, Any]:
    """Parse a JSON request body, returning ``{}`` for an empty body.

    Raises ``HTTPException(422)`` for malformed JSON (instead of the bare
    ``await request.json()`` which surfaced a 500).  When *require_object* is
    set (the default) a non-object top-level value (list, string, number) is
    also a 422, since every endpoint here expects an object payload.
    """
    raw = await request.body()
    if not raw or not raw.strip():
        return {}
    try:
        import json

        data = json.loads(raw)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="Request body is not valid JSON.",
        )
    if require_object and not isinstance(data, dict):
        raise HTTPException(
            status_code=422,
            detail="Request body must be a JSON object.",
        )
    return data


# ---------------------------------------------------------------------------
# B13 — shared response envelope + standard error shape
# ---------------------------------------------------------------------------


def ok(data: Any = None, **extra: Any) -> Dict[str, Any]:
    """Build a success envelope: ``{"ok": true, "data": ...}``.

    Opt-in for new endpoints; existing endpoints that return bare dicts keep
    their shape for backward compatibility with the current UI.
    """
    body: Dict[str, Any] = {"ok": True}
    if data is not None:
        body["data"] = data
    body.update(extra)
    return body


def error_body(status_code: int, message: str, *, code: str = "error",
               error_code: str | None = None, **extra: Any) -> Dict[str, Any]:
    """The canonical error shape shared by every error response (B13).

    Carries the new ``{"ok": false, "error": {...}}`` envelope while preserving
    the legacy ``detail`` / ``error_code`` keys the current UI already reads, so
    the standardization is backward-compatible.
    """
    body: Dict[str, Any] = {
        "ok": False,
        "error": {"code": code, "message": message, "status": status_code},
        # Legacy aliases — the current UI reads these in several places.
        "detail": message,
        "error_code": error_code or code,
    }
    body.update(extra)
    return body


def fail(message: str, status_code: int = 400, *, code: str = "error",
         **extra: Any) -> JSONResponse:
    """Build a standardized error ``JSONResponse`` (opt-in for new endpoints)."""
    return JSONResponse(
        status_code=status_code,
        content=error_body(status_code, message, code=code, **extra),
    )


def jsonable_validation_errors(errors: Any) -> Any:
    """Make pydantic v2 validation errors JSON-serialisable (drop ctx blobs)."""
    out = []
    for e in errors or []:
        if isinstance(e, dict):
            out.append(
                {k: v for k, v in e.items()
                 if k in ("loc", "msg", "type") and _is_jsonable(v)}
            )
    return out


def _is_jsonable(value: Any) -> bool:
    try:
        import json

        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False
