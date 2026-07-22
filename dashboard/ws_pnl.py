"""
Real-time P&L WebSocket (Phase 2, feature 1).

``/ws/pnl`` streams the same live-P&L snapshot served by
``GET /api/live/pnl`` (see :func:`dashboard.live_router.build_pnl_snapshot`)
to connected browsers.  It pushes:

* an immediate snapshot on connect (so the UI paints without waiting a tick);
* a fresh snapshot every ``PNL_PUSH_INTERVAL_OPEN`` seconds while the US market
  is open, and a slower ``PNL_PUSH_INTERVAL_CLOSED`` heartbeat when it is closed.

Each frame carries a ``market_open`` flag so the header can render a pulsing
"LIVE" dot during regular hours and a static "CLOSED" state otherwise.

**Authentication.**  Browser ``WebSocket`` can't set an ``Authorization``
header, so the page first fetches a short-lived signed token from the
auth-guarded ``GET /api/ws/token`` and connects with ``/ws/pnl?token=…``.  The
token is an HMAC over a timestamp keyed by ``DASHBOARD_PASSWORD`` — it keeps raw
credentials out of the URL and expires after :data:`TOKEN_TTL_SECONDS`.  A
same-origin ``Authorization: Basic`` header is also accepted as a fallback, and
when ``DASHBOARD_AUTH_ENABLED`` is off the socket is open (local-dev parity with
the REST guard).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import secrets
import time
from typing import Optional

import contextlib

import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketState

from dashboard.auth import get_settings, require_auth
from dashboard.live_router import build_pnl_snapshot

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

router = APIRouter(tags=["Analytics"])

# Push cadence (seconds).  Module-level so tests can shrink them if needed.
PNL_PUSH_INTERVAL_OPEN = 5.0
PNL_PUSH_INTERVAL_CLOSED = 30.0

TOKEN_TTL_SECONDS = 300  # signed WS token validity window
_WS_CLOSE_POLICY = 1008  # WebSocket close code: policy violation (unauthorized)


# ---------------------------------------------------------------------------
# Signed connect tokens
# ---------------------------------------------------------------------------


def _secret(settings) -> bytes:
    return str(getattr(settings, "DASHBOARD_PASSWORD", "") or "").encode("utf-8")


def make_ws_token(settings, now: Optional[float] = None) -> str:
    """Return a ``<ts>.<hmac>`` token authorizing a WebSocket connect."""
    ts = str(int(now if now is not None else time.time()))
    secret = _secret(settings) or b"ws-anon"
    sig = hmac.new(secret, ts.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def verify_ws_token(settings, token: str, now: Optional[float] = None) -> bool:
    """Constant-time validation of a token from :func:`make_ws_token`."""
    if not token or "." not in token:
        return False
    ts_str, _, sig = token.partition(".")
    if not ts_str.isdigit() or not sig:
        return False
    now = now if now is not None else time.time()
    if abs(now - int(ts_str)) > TOKEN_TTL_SECONDS:
        return False
    secret = _secret(settings) or b"ws-anon"
    expected = hmac.new(secret, ts_str.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _basic_header_ok(settings, header: Optional[str]) -> bool:
    """Validate a same-origin ``Authorization: Basic`` header (fallback path)."""
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False
    user, _, pw = raw.partition(":")
    exp_user = str(getattr(settings, "DASHBOARD_USERNAME", "") or "")
    exp_pw = str(getattr(settings, "DASHBOARD_PASSWORD", "") or "")
    if not exp_pw:
        return False
    return secrets.compare_digest(user, exp_user) and secrets.compare_digest(pw, exp_pw)


def _authorized(websocket: WebSocket, settings) -> bool:
    if not settings.DASHBOARD_AUTH_ENABLED:
        return True
    token = websocket.query_params.get("token", "")
    if token and verify_ws_token(settings, token):
        return True
    return _basic_header_ok(settings, websocket.headers.get("authorization"))


# ---------------------------------------------------------------------------
# Token endpoint + WebSocket
# ---------------------------------------------------------------------------


@router.get("/api/ws/token")
async def ws_token(_user: str = Depends(require_auth)):
    """Issue a short-lived token the browser uses to open ``/ws/pnl``."""
    return {"token": make_ws_token(get_settings()), "ttl": TOKEN_TTL_SECONDS}


def _market_open(settings) -> bool:
    """Best-effort market-open check (mirrors the engine's regular hours)."""
    try:
        from dashboard.ai_commentary import is_market_open

        return bool(is_market_open(settings))
    except Exception:  # noqa: BLE001 — never fail the socket on a clock hiccup
        return False


async def _build_frame() -> dict:
    settings = get_settings()
    open_now = _market_open(settings)
    snapshot = await run_in_threadpool(build_pnl_snapshot, settings)
    return {"type": "pnl", "market_open": open_now, **snapshot}


@router.websocket("/ws/pnl")
async def ws_pnl(websocket: WebSocket) -> None:
    """Stream live P&L snapshots to the browser (see module docstring)."""
    settings = get_settings()
    if not _authorized(websocket, settings):
        await websocket.close(code=_WS_CLOSE_POLICY)
        return

    await websocket.accept()

    # Proactively watch for the client going away.  Without this, a browser that
    # closes its tab (or a half-open TCP connection) is only noticed the *next*
    # time we try to send — and on some disconnects the send is silently dropped
    # by the transport rather than raising, so the push loop would keep firing
    # every interval forever, spamming asyncio's "socket.send() raised exception."
    # warning.  Draining ``receive()`` in parallel surfaces the disconnect
    # immediately so the loop exits the moment the client leaves.  The watcher is
    # started only after the first frame ships, so the initial-frame error path
    # stays a simple accept → build → close.
    disconnected = asyncio.Event()
    watcher: Optional["asyncio.Task[None]"] = None

    async def _watch_disconnect() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            disconnected.set()

    try:
        # Immediate first frame so the UI paints without waiting a tick.
        frame = await _build_frame()
        await websocket.send_json(frame)

        watcher = asyncio.create_task(_watch_disconnect())
        while not disconnected.is_set():
            interval = (
                PNL_PUSH_INTERVAL_OPEN
                if frame.get("market_open")
                else PNL_PUSH_INTERVAL_CLOSED
            )
            # Sleep until the interval elapses *or* the client disconnects,
            # whichever comes first — so a disconnect ends the loop promptly
            # instead of after a full (possibly 30s) sleep.
            try:
                await asyncio.wait_for(disconnected.wait(), timeout=interval)
                break  # disconnected during the wait
            except asyncio.TimeoutError:
                pass  # interval elapsed — time to push the next frame
            if disconnected.is_set() or \
                    websocket.client_state != WebSocketState.CONNECTED:
                break
            frame = await _build_frame()
            await websocket.send_json(frame)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 — client gone / transient; close cleanly
        log.debug("ws_pnl.stream_error", exc_info=True)
    finally:
        disconnected.set()
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(Exception):
                await watcher
        if websocket.client_state == WebSocketState.CONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()
