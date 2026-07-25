"""
Shared authentication helpers for the dashboard and its API routers.

Extracting these out of :mod:`dashboard.app` lets the feature routers
(``watchlist``, ``manual_trade``, ``export`` …) depend on the exact same HTTP
Basic Auth guard without importing the app module (which would be circular).

* :func:`require_auth` — FastAPI dependency enforcing HTTP Basic Auth, honouring
  ``DASHBOARD_AUTH_ENABLED`` and failing closed when no password is configured.
* :func:`verify_admin_password` — constant-time check of the admin password used
  to gate destructive actions (manual trades, engine control, going live).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Optional

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# auto_error=False so we can honour DASHBOARD_AUTH_ENABLED=False (allow with no
# header) and still return a proper 401 challenge when auth is on.
_security = HTTPBasic(auto_error=False)


def _client_ip(request: Optional[Request]) -> str:
    """Best-effort client IP for audit logging.

    Honours ``X-Forwarded-For`` (the dashboard is meant to run behind a
    TLS-terminating reverse proxy) before falling back to the socket peer.
    """
    if request is None:
        return "unknown"
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"


def _log_auth_failure(request: Optional[Request], reason: str, username: str = "") -> None:
    """Emit a WARN-level audit record for a rejected dashboard login."""
    log.warning(
        "auth.failure",
        reason=reason,
        username=username or None,
        ip=_client_ip(request),
        path=request.url.path if request is not None else None,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def get_settings():
    """Resolve settings, preferring ``dashboard.app.get_settings`` when present.

    The dashboard's test-suite monkeypatches ``dashboard.app.get_settings`` to
    point at a controlled ``Settings``; honouring that binding here keeps the
    auth guard consistent with the rest of the app under test.  Falls back to
    the canonical singleton otherwise.
    """
    try:
        from dashboard import app as _app  # local import avoids an import cycle

        return _app.get_settings()
    except Exception:  # noqa: BLE001
        from config.settings import get_settings as _get

        return _get()


def require_auth(
    request: Request,
    credentials: Optional[HTTPBasicCredentials] = Depends(_security),
) -> str:
    """Validate HTTP Basic credentials against the configured dashboard user.

    Uses :func:`secrets.compare_digest` for both the username and password so
    the comparison is constant-time (no early-exit timing side channel).

    * When ``DASHBOARD_AUTH_ENABLED`` is ``False`` the check is skipped
      entirely (intended for trusted local development only).
    * When auth is enabled but ``DASHBOARD_PASSWORD`` is empty the app is
      misconfigured; it fails closed with HTTP 500 rather than granting access.

    Returns:
        The authenticated username.

    Raises:
        HTTPException: 401 when credentials are missing/invalid, 500 when auth
        is enabled but no password is configured.
    """
    settings = get_settings()
    if not settings.DASHBOARD_AUTH_ENABLED:
        return credentials.username if credentials else "anonymous"

    # Brute-force lockout: reject clients that have exhausted their failed-login
    # budget before we even look at the presented credentials.  Lazy import
    # keeps dashboard.rate_limit (which imports this module) cycle-free.
    from dashboard.rate_limit import LoginGuard, client_key

    ckey = client_key(request)
    LoginGuard.check_locked(ckey)

    expected_user = settings.DASHBOARD_USERNAME
    expected_pass = settings.DASHBOARD_PASSWORD

    # A valid session cookie from the branded login page is accepted in place of
    # a Basic header.  Checked before the credential comparison so a logged-in
    # browser never triggers the native credential dialog, and before the
    # missing-credentials branch so it does not burn brute-force budget.  The
    # cookie is signed with a key derived from the password below, so it cannot
    # outlive a password rotation.
    if expected_pass:
        from dashboard.session import COOKIE_NAME, verify_token

        cookie_user = verify_token(request.cookies.get(COOKIE_NAME, ""), expected_pass)
        if cookie_user is not None and secrets.compare_digest(
            cookie_user.encode("utf-8"), expected_user.encode("utf-8")
        ):
            LoginGuard.record_success(ckey)
            return cookie_user

    if not expected_pass:
        # Fail closed: never serve protected content without a real password.
        # A misconfiguration, not a brute-force attempt — don't count it.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Dashboard auth is enabled but DASHBOARD_PASSWORD is not set.",
        )

    if credentials is None:
        # Not a brute-force attempt — nothing was guessed.  A browser whose
        # session cookie expired sends a burst of anonymous polls (quotes,
        # commentary, positions…), and counting those against the failure
        # budget locked legitimate operators out of their own dashboard within
        # a single page load.  Log it, answer 401 (the middleware turns a
        # navigation into the sign-in page), but never spend budget.
        _log_auth_failure(request, "missing_credentials")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Basic"},
        )

    user_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"), expected_user.encode("utf-8")
    )
    pass_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"), expected_pass.encode("utf-8")
    )
    if not (user_ok and pass_ok):
        _log_auth_failure(request, "invalid_credentials", username=credentials.username)
        LoginGuard.record_failure(ckey)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    LoginGuard.record_success(ckey)
    return credentials.username


def verify_admin_password(password: str) -> bool:
    """Return ``True`` when *password* matches the configured admin password.

    Uses the dedicated ``DASHBOARD_ADMIN_PASSWORD`` when set, falling back to
    ``DASHBOARD_PASSWORD`` (see :attr:`config.settings.Settings.admin_password`).
    Constant-time comparison.  Returns ``False`` (never raises) when no admin
    password is configured, so admin-gated actions fail closed.
    """
    expected = get_settings().admin_password
    if not expected:
        return False
    return secrets.compare_digest(
        str(password).encode("utf-8"), expected.encode("utf-8")
    )
