"""
Signed session cookies for the branded dashboard login.

The dashboard has always authenticated with HTTP Basic, which means the only
way into it is the browser's native credential dialog — functional, but it is
the first thing a user sees and it looks nothing like a product.  This module
backs a real login page: :func:`issue_token` mints a signed, expiring session
token after the *same* credential check Basic auth performs, and
:func:`verify_token` validates it so :func:`dashboard.auth.require_auth` can
accept a session cookie as an alternative to a Basic header.

Design notes:

* The signing key is derived from the configured dashboard password, so
  rotating the password invalidates every outstanding session for free.  There
  is no separate secret to provision.
* Tokens carry the username and an absolute expiry, and are signed with
  HMAC-SHA256.  Verification is constant-time and re-checks the expiry, so a
  token cannot be extended by editing the payload.
* Nothing is stored server-side — the token is self-contained, which keeps the
  dashboard's "no database" deployment story intact.

The cookie itself is set HttpOnly + SameSite=Strict by the login route, and the
existing same-origin CSRF guard (:func:`dashboard.middleware.install_csrf_protection`)
already covers every state-changing method, which is what makes cookie auth
safe to add alongside Basic.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Optional

#: Cookie name carrying the signed session token.
COOKIE_NAME = "ustb_session"

#: How long a session stays valid, in seconds (12 hours).
DEFAULT_TTL_SECONDS = 12 * 60 * 60

#: Domain separation for the derived signing key — keeps this HMAC distinct
#: from any other use of the same password elsewhere in the app.
_KEY_INFO = b"ustradingbot.dashboard.session.v1"


def _b64e(raw: bytes) -> str:
    """URL-safe base64 without padding (cookie-safe)."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    """Inverse of :func:`_b64e`, restoring the stripped padding."""
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def signing_key(password: str) -> bytes:
    """Derive the HMAC signing key from the configured dashboard password.

    Returns an empty key when no password is set; callers treat that as "no
    sessions available" and fall through to the normal fail-closed paths.
    """
    if not password:
        return b""
    return hmac.new(_KEY_INFO, password.encode("utf-8"), hashlib.sha256).digest()


def issue_token(
    username: str, password: str, ttl_seconds: int = DEFAULT_TTL_SECONDS
) -> str:
    """Mint a signed session token for *username*.

    *password* is the configured dashboard password, used only to derive the
    signing key — it is never embedded in the token.
    """
    key = signing_key(password)
    if not key:
        return ""
    expires = int(time.time()) + int(ttl_seconds)
    payload = f"{username}|{expires}".encode("utf-8")
    body = _b64e(payload)
    sig = _b64e(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(token: str, password: str) -> Optional[str]:
    """Return the username carried by a valid, unexpired *token*, else ``None``.

    Rejects a malformed token, a bad signature, and an expired one.  Every
    failure path returns ``None`` rather than raising so the caller can simply
    fall through to the Basic-auth challenge.
    """
    key = signing_key(password)
    if not key or not token or "." not in token:
        return None

    body, _, sig = token.partition(".")
    expected = _b64e(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())
    # Constant-time: never leak how much of the signature matched.
    if not hmac.compare_digest(sig, expected):
        return None

    try:
        username, _, expires_raw = _b64d(body).decode("utf-8").partition("|")
        expires = int(expires_raw)
    except (ValueError, UnicodeDecodeError):
        return None

    if not username or expires <= int(time.time()):
        return None
    return username
