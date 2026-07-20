"""
In-process rate limiting and brute-force login lockout for the dashboard.

The dashboard is a single-process FastAPI app fronted by a reverse proxy, so a
lightweight in-memory limiter is sufficient (no Redis / external store).  Two
primitives are provided:

* :func:`rate_limit` — a FastAPI dependency factory guarding an endpoint with a
  sliding-window request cap per client IP.  Used on the money path (manual
  trades, position stops) and the control endpoints (engine, mode, provider,
  backtest).
* :class:`LoginGuard` — tracks failed-authentication attempts per client IP and
  locks the IP out for a configurable window once the failure budget is spent.
  Wired into :func:`dashboard.auth.require_auth`.

Everything is gated by ``Settings.RATE_LIMIT_ENABLED`` so the test-suite (which
sets ``RATE_LIMIT_ENABLED=False`` and, defensively, resets state before each
test via a conftest fixture) is never throttled.  All state is process-local and
thread-safe.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Callable, Deque, Dict, Tuple

from fastapi import HTTPException, Request, status

from dashboard.auth import get_settings

# ---------------------------------------------------------------------------
# Shared state (process-local, thread-safe)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
# bucket name -> {client_key -> deque[timestamps]}
_hits: Dict[str, Dict[str, Deque[float]]] = defaultdict(lambda: defaultdict(deque))
# client_key -> (failure_count, first_failure_ts, locked_until_ts)
_failures: Dict[str, Tuple[int, float, float]] = {}


def reset() -> None:
    """Clear all limiter + lockout state.

    Called from the test-suite between tests so throttling can never leak from
    one test into another.  Safe to call at any time.
    """
    with _lock:
        _hits.clear()
        _failures.clear()


def _client_key(request: Request) -> str:
    """Best-effort client identity for limiting: the peer IP.

    Honours a single ``X-Forwarded-For`` hop (the reverse proxy in front of the
    dashboard) so limits key on the real client rather than the proxy.  Falls
    back to the direct peer, then to a constant when neither is available.
    """
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    client = request.client
    return client.host if client and client.host else "unknown"


def _check_bucket(bucket: str, key: str, limit: int, window: float) -> Tuple[bool, int]:
    """Record a hit and report whether *key* is now over *limit* in *window*.

    Returns ``(allowed, retry_after_seconds)``.  When not allowed, the caller
    should reject with 429 and the returned ``Retry-After``.
    """
    now = time.monotonic()
    cutoff = now - window
    with _lock:
        dq = _hits[bucket][key]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            retry_after = int(dq[0] + window - now) + 1
            return False, max(1, retry_after)
        dq.append(now)
        return True, 0


def rate_limit(
    bucket: str,
    limit: int | None = None,
    per_seconds: float = 60.0,
    *,
    control: bool = False,
) -> Callable:
    """Build a FastAPI dependency enforcing a per-IP sliding-window cap.

    Args:
        bucket: A stable name isolating this endpoint's counters.
        limit: Explicit max hits per window.  When ``None`` the limit is read
            from settings — ``RATE_LIMIT_CONTROL_PER_MIN`` if *control* else
            ``RATE_LIMIT_TRADE_PER_MIN`` — so operators can tune it without code
            changes.
        per_seconds: Window length in seconds (default 60).
        control: Selects the control-endpoint default limit when *limit* is
            ``None``.

    The dependency is a no-op when ``RATE_LIMIT_ENABLED`` is false.
    """

    async def _dep(request: Request) -> None:
        settings = get_settings()
        if not getattr(settings, "RATE_LIMIT_ENABLED", True):
            return
        effective = limit
        if effective is None:
            effective = (
                settings.RATE_LIMIT_CONTROL_PER_MIN
                if control
                else settings.RATE_LIMIT_TRADE_PER_MIN
            )
        allowed, retry_after = _check_bucket(
            bucket, _client_key(request), int(effective), per_seconds
        )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    "Rate limit exceeded — too many requests. "
                    f"Try again in {retry_after}s."
                ),
                headers={"Retry-After": str(retry_after)},
            )

    return _dep


# ---------------------------------------------------------------------------
# Login / auth brute-force lockout
# ---------------------------------------------------------------------------


class LoginGuard:
    """Track failed auth attempts per client IP and enforce a timed lockout.

    The failure budget and lockout duration come from settings
    (``RATE_LIMIT_LOGIN_MAX_FAILURES`` / ``RATE_LIMIT_LOGIN_LOCKOUT_MINUTES``).
    A successful auth clears the client's failure record.
    """

    @staticmethod
    def _enabled() -> bool:
        return bool(getattr(get_settings(), "RATE_LIMIT_ENABLED", True))

    @classmethod
    def check_locked(cls, key: str) -> None:
        """Raise 429 if *key* is currently locked out (else return)."""
        if not cls._enabled():
            return
        now = time.monotonic()
        with _lock:
            rec = _failures.get(key)
            locked_until = rec[2] if rec else 0.0
        if locked_until and now < locked_until:
            retry_after = int(locked_until - now) + 1
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    "Too many failed login attempts — temporarily locked out. "
                    f"Try again in {retry_after}s."
                ),
                headers={"Retry-After": str(retry_after)},
            )

    @classmethod
    def record_failure(cls, key: str) -> None:
        """Record one failed auth for *key*, arming a lockout past the budget."""
        if not cls._enabled():
            return
        settings = get_settings()
        max_failures = int(settings.RATE_LIMIT_LOGIN_MAX_FAILURES)
        lockout = float(settings.RATE_LIMIT_LOGIN_LOCKOUT_MINUTES) * 60.0
        now = time.monotonic()
        with _lock:
            count, first, locked_until = _failures.get(key, (0, now, 0.0))
            # A fresh window starts once a prior lockout has fully elapsed.
            if locked_until and now >= locked_until:
                count, first, locked_until = 0, now, 0.0
            count += 1
            if count >= max_failures:
                locked_until = now + lockout
            _failures[key] = (count, first, locked_until)

    @classmethod
    def record_success(cls, key: str) -> None:
        """Clear the failure record for *key* after a successful auth."""
        with _lock:
            _failures.pop(key, None)


def client_key(request: Request) -> str:
    """Public accessor for the client-identity key (used by the auth guard)."""
    return _client_key(request)
