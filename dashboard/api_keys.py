"""
API key management for the REST API (feature 19).

Keys are minted from the dashboard and returned *once*; only their SHA-256
hash is persisted to ``DATA_DIR/api_keys.json`` (so a leaked file cannot be used
to call the API).  Verification hashes the presented key and compares in
constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Optional

_FILENAME = "api_keys.json"
_PREFIX = "ustb_"


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class ApiKeyInfo:
    """Public metadata about a key (never includes the raw secret)."""

    key_id: str
    name: str
    created_at: str
    prefix: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key_id": self.key_id,
            "name": self.name,
            "created_at": self.created_at,
            "prefix": self.prefix,
        }


class ApiKeyStore:
    """Thread-safe, file-backed store of hashed API keys."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / _FILENAME
        self._lock = RLock()
        self._keys: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not self._path.exists():
                self._keys = {}
                return
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
            self._keys = raw if isinstance(raw, dict) else {}

    def _save(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._keys, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self._data_dir), prefix=".apikeys_", suffix=".tmp")
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

    def create(self, name: str) -> tuple[str, ApiKeyInfo]:
        """Mint a new key.  Returns ``(raw_key, info)`` — the raw key is only
        ever returned here and cannot be recovered later."""
        raw = _PREFIX + secrets.token_urlsafe(32)
        key_id = secrets.token_hex(6)
        created = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            self._keys[key_id] = {
                "name": str(name or "api-key"),
                "hash": _hash_key(raw),
                "created_at": created,
                "prefix": raw[: len(_PREFIX) + 4],
            }
            self._save()
        return raw, ApiKeyInfo(key_id, str(name or "api-key"), created, raw[: len(_PREFIX) + 4])

    def verify(self, raw: str) -> bool:
        """Return whether *raw* matches any stored key (constant-time)."""
        if not raw:
            return False
        candidate = _hash_key(raw)
        with self._lock:
            for record in self._keys.values():
                if hmac.compare_digest(candidate, record.get("hash", "")):
                    return True
        return False

    def revoke(self, key_id: str) -> bool:
        with self._lock:
            if key_id in self._keys:
                del self._keys[key_id]
                self._save()
                return True
            return False

    def list_keys(self) -> List[ApiKeyInfo]:
        with self._lock:
            return [
                ApiKeyInfo(kid, r.get("name", ""), r.get("created_at", ""), r.get("prefix", ""))
                for kid, r in sorted(self._keys.items())
            ]


_STORES: Dict[str, ApiKeyStore] = {}
_STORES_LOCK = RLock()


def get_api_key_store(data_dir: str | Path) -> ApiKeyStore:
    """Return a per-``data_dir`` cached :class:`ApiKeyStore` singleton."""
    key = str(Path(data_dir).resolve())
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = ApiKeyStore(data_dir)
            _STORES[key] = store
        return store
