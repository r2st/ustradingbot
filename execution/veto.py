"""
Signal veto store (P0-3).

A veto blocks new automated entries for a symbol (optionally scoped to one
strategy) until an expiry.  The dashboard / webhook API writes vetoes; the
engine consults :func:`is_vetoed` as an early pre-entry gate, mirroring the
earnings filter — so an operator (or an external system via the webhook) can
say "do not open a new position in NVDA today" without stopping the whole bot.

State is a JSON list under ``DATA_DIR/vetoes.json`` with atomic writes, matching
the price-alert store's conventions.  Expired vetoes are pruned lazily on read.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Optional

import structlog

from config.settings import EASTERN

log = structlog.get_logger(__name__)

_FILENAME = "vetoes.json"
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,6}(\.[A-Z]{1,3})?$")
_MAX_NOTE = 200


class VetoError(ValueError):
    """Raised when a veto operation gets invalid input."""


def _now() -> datetime:
    return datetime.now(tz=EASTERN)


def _normalize_symbol(symbol: str) -> str:
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise VetoError("Symbol must not be empty.")
    if not _SYMBOL_RE.match(sym):
        raise VetoError(f"Invalid symbol: {symbol!r}")
    return sym


class VetoStore:
    """Thread-safe JSON-backed store of active signal vetoes."""

    def __init__(self, data_dir: str | Path) -> None:
        self._dir = Path(data_dir)
        self._path = self._dir / _FILENAME
        self._lock = RLock()

    # -- persistence -------------------------------------------------------
    def _load(self) -> List[Dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("veto.load_failed", error=str(exc))
            return []
        items = data.get("vetoes") if isinstance(data, dict) else data
        return list(items) if isinstance(items, list) else []

    def _save(self, vetoes: List[Dict[str, Any]]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"vetoes": vetoes}, f, indent=2)
            os.replace(tmp, self._path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    @staticmethod
    def _is_active(rule: Dict[str, Any], now: datetime) -> bool:
        exp = rule.get("expires_at")
        if not exp:
            return True
        try:
            expires = datetime.fromisoformat(str(exp))
        except (ValueError, TypeError):
            return True
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=EASTERN)
        return now < expires

    # -- reads -------------------------------------------------------------
    def list_active(self) -> List[Dict[str, Any]]:
        """Return every non-expired veto, pruning expired ones from disk."""
        now = _now()
        with self._lock:
            vetoes = self._load()
            active = [v for v in vetoes if self._is_active(v, now)]
            if len(active) != len(vetoes):
                self._save(active)
            return active

    def is_vetoed(self, symbol: str, strategy: Optional[str] = None) -> bool:
        """Return ``True`` when *symbol* is under an active veto.

        A veto with no ``strategy`` blocks the symbol for every strategy; a
        strategy-scoped veto blocks only that strategy (case-insensitive).
        """
        try:
            sym = _normalize_symbol(symbol)
        except VetoError:
            return False
        strat = (strategy or "").strip().lower()
        for v in self.list_active():
            if str(v.get("symbol", "")).upper() != sym:
                continue
            v_strat = str(v.get("strategy", "") or "").strip().lower()
            if not v_strat or (strat and v_strat == strat):
                return True
        return False

    # -- mutations ---------------------------------------------------------
    def add(
        self,
        symbol: str,
        strategy: str = "",
        ttl_minutes: Optional[float] = None,
        note: str = "",
    ) -> Dict[str, Any]:
        """Add a veto; ``ttl_minutes`` of ``None`` means until end of day (ET)."""
        sym = _normalize_symbol(symbol)
        now = _now()
        if ttl_minutes is None:
            # Default: expire at the next ET midnight (a "for today" veto).
            expires = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        else:
            try:
                ttl = float(ttl_minutes)
            except (TypeError, ValueError):
                raise VetoError("ttl_minutes must be a number.")
            if ttl <= 0:
                raise VetoError("ttl_minutes must be positive.")
            expires = now + timedelta(minutes=ttl)
        rule = {
            "id": uuid.uuid4().hex,
            "symbol": sym,
            "strategy": str(strategy or "").strip().lower(),
            "note": str(note or "")[:_MAX_NOTE],
            "created_at": now.isoformat(timespec="seconds"),
            "expires_at": expires.isoformat(timespec="seconds"),
        }
        with self._lock:
            vetoes = self._load()
            vetoes.append(rule)
            self._save(vetoes)
        log.info("veto.added", symbol=sym, strategy=rule["strategy"],
                 expires_at=rule["expires_at"])
        return rule

    def remove(self, veto_id: str) -> bool:
        with self._lock:
            vetoes = self._load()
            kept = [v for v in vetoes if v.get("id") != veto_id]
            if len(kept) == len(vetoes):
                return False
            self._save(kept)
            return True


_stores: Dict[str, VetoStore] = {}
_stores_lock = RLock()


def get_veto_store(data_dir: str | Path) -> VetoStore:
    key = str(Path(data_dir))
    with _stores_lock:
        store = _stores.get(key)
        if store is None:
            store = VetoStore(data_dir)
            _stores[key] = store
        return store


def is_vetoed(data_dir: str | Path, symbol: str, strategy: Optional[str] = None) -> bool:
    """Convenience wrapper: is *symbol* vetoed in *data_dir*'s store?"""
    return get_veto_store(data_dir).is_vetoed(symbol, strategy)
