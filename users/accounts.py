"""
Multi-user accounts (feature 16).

Separate user accounts, each with their own trading profile — chosen strategies,
capital allocation, and a personal watchlist.  Passwords are hashed with
PBKDF2-HMAC-SHA256 (stdlib ``hashlib`` — no bcrypt/passlib dependency) and a
per-user random salt; only the hash and salt are persisted to
``DATA_DIR/users.json``.

Login issues an opaque, expiring session token held in memory (tokens do not
survive a restart, which is the safe default for a self-hosted dashboard).

The HTTP Basic ``admin`` from the base dashboard config remains a superuser and
is independent of this store.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Optional

_FILENAME = "users.json"
_PBKDF2_ITERATIONS = 240_000
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,32}$")
_TOKEN_TTL_SECONDS = 12 * 3600.0

_VALID_STRATEGIES = {"momentum", "vcp_breakout", "pead", "swing", "mean_reversion"}


class AccountError(ValueError):
    """Raised on invalid registration / login / profile input."""


@dataclass
class UserProfile:
    """Per-user trading configuration."""

    strategies: List[str] = field(default_factory=lambda: ["momentum", "swing"])
    capital: float = 10_000.0
    watchlist: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategies": list(self.strategies),
            "capital": self.capital,
            "watchlist": list(self.watchlist),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "UserProfile":
        d = d or {}
        strategies = [s for s in d.get("strategies", []) if s in _VALID_STRATEGIES]
        return cls(
            strategies=strategies or ["momentum", "swing"],
            capital=float(d.get("capital", 10_000.0) or 0.0),
            watchlist=[str(s).upper() for s in d.get("watchlist", [])],
        )


def _hash_password(password: str, salt: bytes, iterations: int = _PBKDF2_ITERATIONS) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return dk.hex()


def validate_username(username: str) -> str:
    name = str(username or "").strip()
    if not _USERNAME_RE.match(name):
        raise AccountError(
            "Username must be 3-32 chars of letters, digits, '_', '.', or '-'."
        )
    return name


def validate_password(password: str) -> str:
    if len(str(password or "")) < 8:
        raise AccountError("Password must be at least 8 characters.")
    return str(password)


class UserStore:
    """Thread-safe, file-backed store of user accounts + in-memory sessions."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / _FILENAME
        self._lock = RLock()
        self._users: Dict[str, Dict[str, Any]] = {}
        self._sessions: Dict[str, tuple[str, float]] = {}
        self._load()

    # ------------------------------------------------------------------ io

    def _load(self) -> None:
        with self._lock:
            if not self._path.exists():
                self._users = {}
                return
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
            self._users = raw if isinstance(raw, dict) else {}

    def _save(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._users, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self._data_dir), prefix=".users_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, str(self._path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------ registration

    def register(
        self,
        username: str,
        password: str,
        profile: Optional[UserProfile] = None,
    ) -> str:
        """Create a new account.  Returns the normalised username.

        Raises:
            AccountError: on invalid input or a duplicate username.
        """
        name = validate_username(username)
        validate_password(password)
        with self._lock:
            if name in self._users:
                raise AccountError(f"Username {name!r} is taken.")
            salt = secrets.token_bytes(16)
            self._users[name] = {
                "salt": salt.hex(),
                "iterations": _PBKDF2_ITERATIONS,
                "pw_hash": _hash_password(password, salt),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "profile": (profile or UserProfile()).to_dict(),
            }
            self._save()
            return name

    # -------------------------------------------------------------------- auth

    def verify_password(self, username: str, password: str) -> bool:
        """Constant-time password check.  Returns ``False`` for unknown users."""
        with self._lock:
            record = self._users.get(str(username).strip())
        if record is None:
            # Still do work to blunt user-enumeration timing.
            _hash_password(password, b"decoy-salt-000000")
            return False
        salt = bytes.fromhex(record["salt"])
        iterations = int(record.get("iterations", _PBKDF2_ITERATIONS))
        candidate = _hash_password(password, salt, iterations)
        return hmac.compare_digest(candidate, record["pw_hash"])

    def login(self, username: str, password: str) -> str:
        """Verify credentials and return a fresh session token.

        Raises:
            AccountError: when the credentials are invalid.
        """
        name = str(username).strip()
        if not self.verify_password(name, password):
            raise AccountError("Invalid username or password.")
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = (name, time.monotonic() + _TOKEN_TTL_SECONDS)
        return token

    def validate_token(self, token: str) -> Optional[str]:
        """Return the username for a valid, unexpired *token*, else ``None``."""
        if not token:
            return None
        with self._lock:
            entry = self._sessions.get(token)
            if entry is None:
                return None
            username, expiry = entry
            if time.monotonic() > expiry:
                self._sessions.pop(token, None)
                return None
            return username

    def logout(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    # ---------------------------------------------------------------- profiles

    def exists(self, username: str) -> bool:
        with self._lock:
            return str(username).strip() in self._users

    def list_users(self) -> List[str]:
        with self._lock:
            return sorted(self._users.keys())

    def get_profile(self, username: str) -> Optional[UserProfile]:
        with self._lock:
            record = self._users.get(str(username).strip())
        if record is None:
            return None
        return UserProfile.from_dict(record.get("profile", {}))

    def update_profile(self, username: str, profile: UserProfile) -> UserProfile:
        name = str(username).strip()
        with self._lock:
            if name not in self._users:
                raise AccountError(f"Unknown user {name!r}.")
            self._users[name]["profile"] = profile.to_dict()
            self._save()
            return profile


# ---------------------------------------------------------------------------
# Process-wide accessor
# ---------------------------------------------------------------------------

_STORES: Dict[str, UserStore] = {}
_STORES_LOCK = RLock()


def get_user_store(data_dir: str | Path) -> UserStore:
    """Return a per-``data_dir`` cached :class:`UserStore` singleton."""
    key = str(Path(data_dir).resolve())
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = UserStore(data_dir)
            _STORES[key] = store
        return store
