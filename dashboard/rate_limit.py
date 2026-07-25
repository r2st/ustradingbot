"""
In-process rate limiting and brute-force login lockout for the dashboard.

By default the dashboard runs as a single uvicorn process fronted by a reverse
proxy, so a lightweight in-memory limiter is sufficient.  When the deployment
scales to multiple workers, set ``RATE_LIMIT_REDIS_URL`` to a shared Redis and
both the request cap and the login lockout key their state there instead, so the
caps stay correct cluster-wide (audit B-4).  If you scale to >1 worker *without*
a shared store, :func:`warn_if_multiworker` logs a loud startup WARN rather than
silently letting the effective cap become ``N×``.  Two primitives are provided:

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

import os
import threading
import time
from collections import defaultdict, deque
from typing import Any, Callable, Deque, Dict, Optional, Tuple

import structlog
from fastapi import HTTPException, Request, status

from dashboard.auth import get_settings

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Shared state (process-local, thread-safe)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
# bucket name -> {client_key -> deque[timestamps]}
_hits: Dict[str, Dict[str, Deque[float]]] = defaultdict(lambda: defaultdict(deque))
# client_key -> (failure_count, first_failure_ts, locked_until_ts, lockout_rounds)
# ``lockout_rounds`` counts consecutive lockouts and drives the escalating
# lockout length (see :func:`_lockout_seconds`).
_failures: Dict[str, Tuple[int, float, float, int]] = {}

# ---------------------------------------------------------------------------
# Optional shared (Redis) backend — keeps caps correct across workers (B-4)
# ---------------------------------------------------------------------------
# When a Redis client is configured the sliding-window counters and the login
# lockout key their state in Redis instead of the process-local dicts above, so
# ``uvicorn --workers N`` no longer multiplies the effective limit by N.  The
# client is duck-typed (only ``incr``/``expire``/``ttl``/``set``/``exists``/
# ``delete`` are used) so it works with ``redis-py`` or an injected fake.
_redis: Optional[Any] = None
_redis_lock = threading.Lock()


def set_redis_client(client: Optional[Any]) -> None:
    """Install (or clear) the shared Redis client used by the limiter."""
    global _redis
    with _redis_lock:
        _redis = client


def configure_backend(settings: Any = None) -> Optional[Any]:
    """Wire up the Redis backend from ``RATE_LIMIT_REDIS_URL`` if configured.

    Returns the active client (or ``None`` for in-process mode).  Idempotent and
    fail-safe: a missing ``redis`` package or an unreachable server logs a WARN
    and falls back to the in-process limiter rather than breaking startup.
    """
    settings = settings or get_settings()
    url = str(getattr(settings, "RATE_LIMIT_REDIS_URL", "") or "").strip()
    if not url:
        return _redis
    if _redis is not None:
        return _redis
    try:
        import redis  # type: ignore

        client = redis.Redis.from_url(url, decode_responses=True)
        client.ping()
        set_redis_client(client)
        log.info("rate_limit.redis_backend_enabled", url=url)
    except Exception as exc:  # noqa: BLE001 — never fail startup over the limiter
        log.warning(
            "rate_limit.redis_backend_unavailable",
            url=url, error=str(exc),
            detail="falling back to in-process limiter",
        )
    return _redis


def _worker_count() -> int:
    """Best-effort configured worker count from common env vars."""
    for env in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS"):
        val = os.environ.get(env)
        if val and val.isdigit():
            return int(val)
    return 1


def warn_if_multiworker(settings: Any = None) -> None:
    """Log a WARN when running >1 worker without a shared limiter store (B-4).

    A scaling change (``--workers N``) silently weakens the rate limiter and
    brute-force lockout unless a shared store is configured; surface it loudly at
    startup so the weakening is never silent.
    """
    workers = _worker_count()
    if workers > 1 and _redis is None:
        log.warning(
            "rate_limit.multiworker_without_shared_store",
            workers=workers,
            detail=(
                "The rate limiter and login lockout are process-local; with "
                f"{workers} workers the effective cap is ~{workers}x the "
                "configured value and lockout can be sidestepped. Set "
                "RATE_LIMIT_REDIS_URL to a shared Redis, or run a single worker."
            ),
        )


def reset() -> None:
    """Clear all limiter + lockout state.

    Called from the test-suite between tests so throttling can never leak from
    one test into another.  Safe to call at any time.  Clears both the
    in-process dicts and — when configured — the Redis keys under our prefixes.
    """
    with _lock:
        _hits.clear()
        _failures.clear()
    if _redis is not None:
        try:
            for pattern in ("rl:*", "rlf:*", "rll:*", "rlr:*"):
                keys = list(_redis.scan_iter(match=pattern))
                if keys:
                    _redis.delete(*keys)
        except Exception:  # noqa: BLE001 — reset is best-effort in tests
            pass


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
    should reject with 429 and the returned ``Retry-After``.  Delegates to the
    shared Redis store when one is configured (B-4); otherwise uses the
    process-local sliding window.
    """
    if _redis is not None:
        return _check_bucket_redis(bucket, key, limit, window)
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


def _check_bucket_redis(bucket: str, key: str, limit: int, window: float) -> Tuple[bool, int]:
    """Fixed-window per-worker-shared counter in Redis.

    A fixed window (``INCR`` + ``EXPIRE`` on a ``rl:{bucket}:{key}:{window_id}``
    key) is used rather than a sorted-set sliding window so the shared-store
    command surface stays tiny — every worker increments the *same* key, so the
    cap is enforced cluster-wide.  Fails open on a Redis error (never 500s a
    request over a limiter hiccup).
    """
    try:
        window_id = int(time.time() // window)
        rkey = f"rl:{bucket}:{key}:{window_id}"
        count = int(_redis.incr(rkey))
        if count == 1:
            _redis.expire(rkey, int(window) + 1)
        if count > limit:
            ttl = int(_redis.ttl(rkey) or 0)
            return False, max(1, ttl)
        return True, 0
    except Exception:  # noqa: BLE001 — fail open on backend trouble
        log.warning("rate_limit.redis_check_failed", bucket=bucket, exc_info=True)
        return True, 0


def rate_limit(
    bucket: str,
    limit: int | None = None,
    per_seconds: float = 60.0,
    *,
    control: bool = False,
    ai: bool = False,
) -> Callable:
    """Build a FastAPI dependency enforcing a per-IP sliding-window cap.

    Args:
        bucket: A stable name isolating this endpoint's counters.
        limit: Explicit max hits per window.  When ``None`` the limit is read
            from settings — ``RATE_LIMIT_AI_PER_MIN`` if *ai*,
            ``RATE_LIMIT_CONTROL_PER_MIN`` if *control*, else
            ``RATE_LIMIT_TRADE_PER_MIN`` — so operators can tune it without code
            changes.
        per_seconds: Window length in seconds (default 60).
        control: Selects the control-endpoint default limit when *limit* is
            ``None``.
        ai: Selects the AI/LLM-endpoint default limit when *limit* is ``None``
            (takes precedence over *control*).  These endpoints drive paid
            OpenRouter calls, so they get their own, tighter cap (audit B-1).

    The dependency is a no-op when ``RATE_LIMIT_ENABLED`` is false.
    """

    async def _dep(request: Request) -> None:
        settings = get_settings()
        if not getattr(settings, "RATE_LIMIT_ENABLED", True):
            return
        effective = limit
        if effective is None:
            if ai:
                effective = getattr(settings, "RATE_LIMIT_AI_PER_MIN", 12)
            elif control:
                effective = settings.RATE_LIMIT_CONTROL_PER_MIN
            else:
                effective = settings.RATE_LIMIT_TRADE_PER_MIN
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


def _lockout_policy(settings: Any) -> Tuple[int, float, float]:
    """Return ``(max_failures, base_lockout_s, cap_lockout_s)`` from settings."""
    max_failures = int(settings.RATE_LIMIT_LOGIN_MAX_FAILURES)
    cap = float(settings.RATE_LIMIT_LOGIN_LOCKOUT_MINUTES) * 60.0
    base = float(getattr(settings, "RATE_LIMIT_LOGIN_LOCKOUT_SECONDS", 60) or 60)
    return max_failures, min(base, cap) if cap > 0 else base, cap


def _decay_window(cap: float) -> float:
    """How long a client must stay quiet before its failure history is forgotten."""
    return max(cap, 300.0) * 2.0


def _lockout_seconds(rounds: int, base: float, cap: float) -> float:
    """Escalating lockout length: *base*, 2×, 4×… capped at *cap*.

    ``rounds`` is the number of consecutive lockouts this client has earned, so
    the first offence costs ``base`` (a minute) and a client that keeps guessing
    reaches the ceiling within a few rounds.
    """
    span = base * (2 ** max(0, rounds - 1))
    return min(span, cap) if cap > 0 else span


class LoginGuard:
    """Track failed auth attempts per client IP and enforce a timed lockout.

    The failure budget and lockout length come from settings
    (``RATE_LIMIT_LOGIN_MAX_FAILURES`` / ``RATE_LIMIT_LOGIN_LOCKOUT_SECONDS`` /
    ``RATE_LIMIT_LOGIN_LOCKOUT_MINUTES`` as the escalation ceiling).  A
    successful auth clears the client's failure record.

    Two properties keep the guard from trapping the legitimate operator:

    * only *presented and wrong* credentials are recorded — the caller must not
      report an anonymous request as a failure (see :func:`dashboard.auth.
      require_auth`);
    * attempts made *during* an active lockout are refused without extending it,
      so a browser that keeps polling (or a user who keeps hitting refresh)
      cannot push their own unlock time perpetually into the future.
    """

    @staticmethod
    def _enabled() -> bool:
        return bool(getattr(get_settings(), "RATE_LIMIT_ENABLED", True))

    @classmethod
    def check_locked(cls, key: str) -> None:
        """Raise 429 if *key* is currently locked out (else return)."""
        if not cls._enabled():
            return
        if _redis is not None:
            retry_after = cls._redis_locked_ttl(key)
            if retry_after <= 0:
                return
        else:
            now = time.monotonic()
            with _lock:
                rec = _failures.get(key)
                locked_until = rec[2] if rec else 0.0
            if not (locked_until and now < locked_until):
                return
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
        """Record one *wrong-credential* auth for *key*, locking past the budget.

        Callers must only reach here when credentials were actually presented
        and rejected; an anonymous request is not a guess and must not spend
        budget.  An attempt made while the client is already locked out is
        ignored rather than restarting the clock.
        """
        if not cls._enabled():
            return
        max_failures, base, cap = _lockout_policy(get_settings())
        if _redis is not None:
            cls._redis_record_failure(key, max_failures, base, cap)
            return
        now = time.monotonic()
        with _lock:
            count, last, locked_until, rounds = _failures.get(key, (0, now, 0.0, 0))
            if locked_until and now < locked_until:
                return  # already locked — never extend an active lockout
            if (now - last) > _decay_window(cap):
                # Quiet for a full decay window: forget the count *and* the
                # escalation ladder, so today's typo costs the short lockout
                # rather than inheriting last month's.
                count, rounds = 0, 0
            elif locked_until:
                # A prior lockout was served: fresh budget, ladder retained.
                count = 0
            count += 1
            locked_until = 0.0
            if count >= max_failures:
                rounds += 1
                locked_until = now + _lockout_seconds(rounds, base, cap)
            _failures[key] = (count, now, locked_until, rounds)

    @classmethod
    def record_success(cls, key: str) -> None:
        """Clear the failure record for *key* after a successful auth."""
        if _redis is not None:
            try:
                _redis.delete(f"rlf:{key}", f"rll:{key}", f"rlr:{key}")
            except Exception:  # noqa: BLE001 — fail open
                log.warning("rate_limit.redis_success_failed", exc_info=True)
            return
        with _lock:
            _failures.pop(key, None)

    # -- Redis-backed lockout helpers (shared across workers) ---------------

    @staticmethod
    def _redis_locked_ttl(key: str) -> int:
        """Return the remaining lockout seconds for *key* (0 if not locked)."""
        try:
            if not _redis.exists(f"rll:{key}"):
                return 0
            return max(1, int(_redis.ttl(f"rll:{key}") or 1))
        except Exception:  # noqa: BLE001 — fail open (never lock on backend error)
            log.warning("rate_limit.redis_lock_check_failed", exc_info=True)
            return 0

    @staticmethod
    def _redis_record_failure(
        key: str, max_failures: int, base: float, cap: float
    ) -> None:
        """Increment the shared failure counter and arm a lockout past budget.

        Mirrors the in-process policy: an attempt during an active lockout is
        ignored (never extends it), and consecutive lockouts escalate via the
        ``rlr:`` round counter, which expires on its own so a quiet client
        decays back to the short first lockout.
        """
        try:
            if _redis.exists(f"rll:{key}"):
                return  # already locked — never extend an active lockout
            cnt_key = f"rlf:{key}"
            count = int(_redis.incr(cnt_key))
            if count == 1:
                # Track failures over a rolling window at least as long as the
                # lockout so a slow trickle still accumulates toward the budget.
                _redis.expire(cnt_key, int(_decay_window(cap)))
            if count >= max_failures:
                rounds = int(_redis.incr(f"rlr:{key}"))
                _redis.expire(f"rlr:{key}", int(cap + _decay_window(cap)))
                lockout = _lockout_seconds(rounds, base, cap)
                _redis.set(f"rll:{key}", "1", ex=int(lockout) + 1)
                # Start the next budget clean once the lockout expires.
                _redis.delete(cnt_key)
        except Exception:  # noqa: BLE001 — fail open
            log.warning("rate_limit.redis_record_failure_failed", exc_info=True)


def client_key(request: Request) -> str:
    """Public accessor for the client-identity key (used by the auth guard)."""
    return _client_key(request)
