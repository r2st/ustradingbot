"""
Multi-user registration / login API (feature 16).

Only active when ``MULTI_USER_ENABLED``.  Login returns an opaque bearer token
that the client stores and presents as ``X-User-Token`` to read/update its own
profile.  The HTTP Basic admin remains independent and always available.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from config.settings import get_settings
from dashboard.auth import require_auth
from dashboard.http_util import parse_json_body
from users.accounts import AccountError, UserProfile, get_user_store

router = APIRouter(prefix="/api/users", tags=["Users"])


def _store():
    return get_user_store(get_settings().DATA_DIR)


def _require_multi_user() -> None:
    if not getattr(get_settings(), "MULTI_USER_ENABLED", False):
        raise HTTPException(status_code=404, detail="Multi-user support is disabled.")


async def _body(request: Request) -> Dict[str, Any]:
    """Parse a JSON object body, 422 on malformed JSON (see B7)."""
    return await parse_json_body(request)


def _current_user(x_user_token: Optional[str]) -> str:
    username = _store().validate_token(x_user_token or "")
    if username is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session token.")
    return username


@router.post("/register")
async def register(request: Request):
    _require_multi_user()
    body = await _body(request)
    profile = UserProfile.from_dict(body.get("profile", {})) if body.get("profile") else None
    try:
        name = _store().register(str(body.get("username", "")),
                                 str(body.get("password", "")), profile)
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "username": name}


@router.post("/login")
async def login(request: Request):
    _require_multi_user()
    body = await _body(request)
    try:
        token = _store().login(str(body.get("username", "")), str(body.get("password", "")))
    except AccountError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    return {"ok": True, "token": token}


@router.post("/logout")
async def logout(x_user_token: Optional[str] = Header(default=None)):
    _require_multi_user()
    _store().logout(x_user_token or "")
    return {"ok": True}


@router.get("/me")
async def me(x_user_token: Optional[str] = Header(default=None)):
    _require_multi_user()
    username = _current_user(x_user_token)
    profile = _store().get_profile(username)
    return {"username": username, "profile": profile.to_dict() if profile else None}


@router.post("/me/profile")
async def update_profile(request: Request, x_user_token: Optional[str] = Header(default=None)):
    _require_multi_user()
    username = _current_user(x_user_token)
    body = await _body(request)
    try:
        profile = _store().update_profile(username, UserProfile.from_dict(body))
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "username": username, "profile": profile.to_dict()}


@router.get("")
async def list_users(_user: str = Depends(require_auth)):
    """Admin-only: list registered usernames (HTTP Basic auth)."""
    _require_multi_user()
    return {"users": _store().list_users()}
