"""
Multi-user registration / login API (feature 16).

Only active when ``MULTI_USER_ENABLED``.  Login returns an opaque bearer token
that the client stores and presents as ``X-User-Token`` to read/update its own
profile.  The HTTP Basic admin remains independent and always available.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException

from config.settings import get_settings
from dashboard.auth import require_auth
from dashboard.schemas import LoginRequest, ProfileUpdateRequest, RegisterRequest
from users.accounts import AccountError, UserProfile, get_user_store

router = APIRouter(prefix="/api/users", tags=["Users"])


def _store():
    return get_user_store(get_settings().DATA_DIR)


def _require_multi_user() -> None:
    if not getattr(get_settings(), "MULTI_USER_ENABLED", False):
        raise HTTPException(status_code=404, detail="Multi-user support is disabled.")


def _current_user(x_user_token: Optional[str]) -> str:
    username = _store().validate_token(x_user_token or "")
    if username is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session token.")
    return username


@router.post("/register")
async def register(payload: RegisterRequest):
    _require_multi_user()
    profile = UserProfile.from_dict(payload.profile) if payload.profile else None
    try:
        name = _store().register(payload.username, payload.password, profile)
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "username": name}


@router.post("/login")
async def login(payload: LoginRequest):
    _require_multi_user()
    try:
        token = _store().login(payload.username, payload.password)
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
async def update_profile(
    payload: ProfileUpdateRequest,
    x_user_token: Optional[str] = Header(default=None),
):
    _require_multi_user()
    username = _current_user(x_user_token)
    try:
        profile = _store().update_profile(
            username, UserProfile.from_dict(payload.model_dump())
        )
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "username": username, "profile": profile.to_dict()}


@router.get("")
async def list_users(_user: str = Depends(require_auth)):
    """Admin-only: list registered usernames (HTTP Basic auth)."""
    _require_multi_user()
    return {"users": _store().list_users()}
