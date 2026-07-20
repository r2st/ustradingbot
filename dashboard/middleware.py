"""
Cross-cutting HTTP middleware and error handling for the dashboard app.

Extracted from :mod:`dashboard.app` so the concerns stay in one auditable place:

* :func:`install_request_logging` (B9) — a middleware that binds a per-request
  correlation id into :mod:`structlog` contextvars and logs one structured line
  per request (method, path, status, latency).  The id is also echoed back on
  the ``X-Request-ID`` response header so a client can quote it in a bug report.
* :func:`install_security_headers` (B4) — a middleware that stamps the standard
  browser hardening headers (frame options, nosniff, HSTS, CSP, referrer
  policy) onto every response.
* :func:`install_exception_handlers` (B8) — a catch-all handler that logs the
  failure with request context and returns a consistent ``{detail, error_code}``
  JSON envelope, never leaking a stack trace to the client in production.

:data:`OPENAPI_TAGS` (B12) groups the API's endpoints by functional area for a
readable ``/docs`` page.
"""

from __future__ import annotations

import time
import uuid

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from dashboard.http_util import (
    BodySizeLimitMiddleware,
    error_body,
    jsonable_validation_errors,
)

log: structlog.stdlib.BoundLogger = structlog.get_logger("dashboard.access")

# ---------------------------------------------------------------------------
# OpenAPI tag metadata (B12) — grouping endpoints by functional area
# ---------------------------------------------------------------------------
#: Ordered tag descriptions surfaced on the ``/docs`` and ``/redoc`` pages.  The
#: order here is the order the groups render in.
OPENAPI_TAGS: list[dict[str, str]] = [
    {"name": "System", "description": "Health, trading mode, and dashboard root."},
    {"name": "Trading", "description": "Manual trades, position stops, and engine control — money-moving, admin-gated actions."},
    {"name": "Analytics", "description": "Performance, risk, history, and Monte-Carlo insight endpoints (read-only)."},
    {"name": "Market Data", "description": "Provider selection/status, quotes, pre-market and sector scans."},
    {"name": "Signals & TA", "description": "Technical-analysis charts, rationale, and signal insights."},
    {"name": "AI", "description": "Live analyst commentary and the AI memory / learnings layer."},
    {"name": "Configuration", "description": "Watchlists, universe, trade-selection, and alert-rule configuration."},
    {"name": "Journal", "description": "Trade notes, activity log, and exports."},
    {"name": "Backtesting", "description": "On-demand and scheduled strategy backtests."},
    {"name": "Users", "description": "Multi-user registration, login, and profiles."},
    {"name": "Notifications", "description": "Alert channels, push subscriptions, and test dispatch."},
    {"name": "rest-api-v1", "description": "Versioned, API-key-authenticated JSON API under /api/v1 (external integrations)."},
]


def _client_ip(request: Request) -> str:
    """Best-effort client IP, honouring ``X-Forwarded-For`` behind a proxy."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _route_label(request: Request) -> str:
    """A low-cardinality route label for metrics (the matched path template).

    Uses the resolved route pattern (``/api/backtest/status/{job_id}``) rather
    than the concrete path so per-id URLs don't explode the metric cardinality.
    Falls back to the raw path when no route matched (404s).
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or request.url.path


def _record_request_metrics(request: Request, status_code: int, duration_ms: float) -> None:
    """Record request count, error count, and latency into the metrics registry.

    Best-effort — a metrics hiccup must never disturb the request path.
    """
    try:
        from dashboard import metrics

        labels = {"method": request.method, "route": _route_label(request),
                  "status": str(status_code)}
        metrics.inc(
            "ustb_http_requests_total", labels=labels,
            help_text="Total HTTP requests handled, by method/route/status.",
        )
        if status_code >= 500:
            metrics.inc(
                "ustb_http_errors_total",
                labels={"method": request.method, "route": _route_label(request)},
                help_text="HTTP responses with a 5xx status.",
            )
        metrics.observe(
            "ustb_http_request_duration_ms", duration_ms,
            labels={"route": _route_label(request)},
            help_text="HTTP request latency in milliseconds, by route.",
        )
    except Exception:  # noqa: BLE001 — telemetry must never break the request
        pass


# ---------------------------------------------------------------------------
# B9 — request logging + correlation id
# ---------------------------------------------------------------------------
def install_request_logging(app: FastAPI) -> None:
    """Register the request-logging / request-id middleware on *app*."""

    @app.middleware("http")
    async def _request_logging(request: Request, call_next):
        # Honour an inbound correlation id (from a proxy / client) or mint one.
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        start = time.perf_counter()
        response = None
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            # Echo the correlation id back so a client can quote it in reports.
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000.0, 2)
            _record_request_metrics(request, status_code, duration_ms)
            # /health is polled constantly by monitors — keep it at debug so it
            # doesn't drown the access log.
            emit = log.debug if request.url.path == "/health" else log.info
            emit(
                "request",
                status=status_code,
                duration_ms=duration_ms,
                ip=_client_ip(request),
            )
            structlog.contextvars.unbind_contextvars(
                "request_id", "method", "path"
            )


# ---------------------------------------------------------------------------
# B4 — security headers
# ---------------------------------------------------------------------------
#: Content-Security-Policy for the dashboard SPA.  Scripts/styles are served
#: same-origin (self-hosted Chart.js, no CDN); ``connect-src 'self'`` covers the
#: JSON API + the same-origin WebSocket (``ws:``/``wss:``).  ``'unsafe-inline'``
#: is allowed for styles and scripts because the templates carry inline
#: ``<style>``/bootstrap ``<script>`` blocks; keeping the origin locked to
#: ``'self'`` is the meaningful control here.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self' ws: wss:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "X-XSS-Protection": "1; mode=block",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": _CSP,
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


def install_security_headers(app: FastAPI) -> None:
    """Register the security-headers middleware on *app*."""

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


# ---------------------------------------------------------------------------
# B5(v2) — CSRF: same-origin check on state-changing requests
# ---------------------------------------------------------------------------
#: Methods that can change server state and therefore need CSRF protection.
_CSRF_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _origin_host(value: str) -> str:
    """Return the lower-cased ``host[:port]`` of an Origin/Referer URL."""
    from urllib.parse import urlsplit

    if not value:
        return ""
    return (urlsplit(value).netloc or "").lower()


def _request_hosts(request: Request) -> set[str]:
    """The set of host[:port] values that count as same-origin for *request*.

    Includes the ``Host`` header and any ``X-Forwarded-Host`` (the dashboard is
    meant to sit behind a TLS-terminating reverse proxy), plus operator-trusted
    origins from settings.
    """
    hosts: set[str] = set()
    for hv in (request.headers.get("host"), request.headers.get("x-forwarded-host")):
        if hv:
            hosts.add(hv.split(",")[0].strip().lower())
    return hosts


def install_csrf_protection(app: FastAPI) -> None:
    """Register a same-origin CSRF guard for state-changing requests (B-5).

    For POST/PUT/PATCH/DELETE, when the browser sends an ``Origin`` (or falls
    back to ``Referer``) it must match the request's own host or an operator-
    trusted origin; a cross-site value is rejected with 403.  Requests with
    *neither* header (non-browser API clients such as the API-key-authenticated
    ``/api/v1`` surface, curl, health probes) are allowed — browsers always send
    ``Origin`` on cross-origin state-changing requests, so their absence is not a
    cross-site attack.  Gated by ``CSRF_PROTECTION_ENABLED``.
    """

    @app.middleware("http")
    async def _csrf(request: Request, call_next):
        if request.method in _CSRF_METHODS:
            from dashboard.auth import get_settings

            settings = get_settings()
            if getattr(settings, "CSRF_PROTECTION_ENABLED", True):
                origin = request.headers.get("origin") or ""
                referer = request.headers.get("referer") or ""
                source = _origin_host(origin) or _origin_host(referer)
                if source:
                    allowed = _request_hosts(request)
                    allowed |= getattr(settings, "csrf_trusted_origin_hosts", set())
                    if source not in allowed:
                        log.warning(
                            "csrf.blocked",
                            origin=origin or None,
                            referer=referer or None,
                            source=source,
                            allowed=sorted(allowed),
                            path=request.url.path,
                            ip=_client_ip(request),
                        )
                        from dashboard.http_util import error_body

                        return JSONResponse(
                            status_code=403,
                            content=error_body(
                                403,
                                "Cross-site request blocked (origin mismatch).",
                                code="csrf_failed",
                            ),
                        )
        return await call_next(request)


# ---------------------------------------------------------------------------
# B8 — global exception handler
# ---------------------------------------------------------------------------
def install_exception_handlers(app: FastAPI, *, debug: bool = False) -> None:
    """Register the error handlers on *app* (B8 + B13).

    Every error — a raised ``HTTPException``, a request-validation failure, or
    an unexpected bug — is rendered through the single :func:`error_body`
    envelope (``{"ok": false, "error": {...}}``) so the whole API answers with
    one consistent shape.  The legacy ``detail`` / ``error_code`` keys are kept
    alongside so existing UI code keeps working.

    Args:
        app: The FastAPI application.
        debug: When ``True`` the 500 envelope includes the exception message
            (developer convenience).  In production (the default) the client
            gets a generic message and no stack trace; the detail is logged.
    """

    def _with_request_id(body: dict) -> dict:
        request_id = structlog.contextvars.get_contextvars().get("request_id")
        if request_id:
            body["request_id"] = request_id
        return body

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(_request: Request, exc: StarletteHTTPException):
        # Deliberate, already-shaped responses (401/403/404/413/422/…).  Reshape
        # them into the shared envelope while preserving their status + detail.
        detail = exc.detail
        message = detail if isinstance(detail, str) else "Request failed."
        extra = {} if isinstance(detail, str) else {"errors": detail}
        return JSONResponse(
            status_code=exc.status_code,
            content=_with_request_id(
                error_body(exc.status_code, message, code="http_error", **extra)
            ),
            headers=getattr(exc, "headers", None) or None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(_request: Request, exc: RequestValidationError):
        code = 422
        return JSONResponse(
            status_code=code,
            content=_with_request_id(
                error_body(
                    code, "Request validation failed.",
                    code="validation_error",
                    errors=jsonable_validation_errors(exc.errors()),
                )
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Let FastAPI/Starlette's own HTTPException handling run above — those
        # are deliberate responses, not bugs.
        if isinstance(exc, (FastAPIHTTPException, StarletteHTTPException)):
            raise exc
        log.error(
            "unhandled_exception",
            error=str(exc),
            error_type=type(exc).__name__,
            path=request.url.path,
            method=request.method,
            ip=_client_ip(request),
            exc_info=exc,
        )
        message = (
            str(exc) if debug
            else "Internal server error. The incident has been logged."
        )
        return JSONResponse(
            status_code=500,
            content=_with_request_id(
                error_body(500, message, code="internal_error")
            ),
        )


def install(app: FastAPI, *, debug: bool = False) -> None:
    """Install every cross-cutting concern on *app* in the right order.

    Middleware runs in the reverse of registration order for the response leg,
    so security headers are added last (registered first) to wrap everything,
    and request logging is registered last so it is the outermost layer and
    times the whole stack.
    """
    # B5 — reject over-sized request bodies before any handler buffers them.
    app.add_middleware(BodySizeLimitMiddleware)
    install_security_headers(app)
    # B5(v2) — same-origin CSRF guard (registered before the exception handlers
    # so a blocked request still gets the standard error envelope).
    install_csrf_protection(app)
    install_exception_handlers(app, debug=debug)
    install_request_logging(app)
