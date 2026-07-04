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
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from config.settings import get_settings

# auto_error=False so we can honour DASHBOARD_AUTH_ENABLED=False (allow with no
# header) and still return a proper 401 challenge when auth is on.
_security = HTTPBasic(auto_error=False)


def require_auth(
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

    expected_user = settings.DASHBOARD_USERNAME
    expected_pass = settings.DASHBOARD_PASSWORD

    if not expected_pass:
        # Fail closed: never serve protected content without a real password.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Dashboard auth is enabled but DASHBOARD_PASSWORD is not set.",
        )

    if credentials is None:
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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def verify_admin_password(password: str) -> bool:
    """Return ``True`` when *password* matches the configured admin password.

    Constant-time comparison.  Returns ``False`` (never raises) when no admin
    password is configured, so admin-gated actions fail closed.
    """
    expected = get_settings().DASHBOARD_PASSWORD
    if not expected:
        return False
    return secrets.compare_digest(
        str(password).encode("utf-8"), expected.encode("utf-8")
    )
