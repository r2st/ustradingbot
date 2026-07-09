"""
Time-to-live cache for AI veto verdicts.

The AI news-veto layer (:mod:`ai.analyst`) is the only paid step in the
signal pipeline.  Because the screener re-evaluates the same symbols every
scan cycle, an uncached AI layer would fire redundant API calls for the same
stock many times per day.  This module caches each verdict for a configurable
TTL (default 4 hours) keyed by ``symbol + strategy`` so a repeat evaluation
within the window is free.

The cache is persisted to ``ai_cache.json`` under ``settings.DATA_DIR`` so it
survives restarts.  Writes are atomic (temp file + ``os.replace``).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import structlog

from config.settings import EASTERN

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class AICache:
    """Persistent TTL cache for AI verdicts keyed by ``symbol:strategy``.

    Attributes:
        ttl: How long an entry stays valid.
        path: Location of the JSON cache file.
    """

    def __init__(self, data_dir: str, ttl_hours: float = 4.0) -> None:
        """Initialise the cache.

        Args:
            data_dir: Directory where ``ai_cache.json`` is stored.
            ttl_hours: Entry lifetime in hours.
        """
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self.path: Path = self._data_dir / "ai_cache.json"
        self.ttl = timedelta(hours=ttl_hours)
        self._store: Dict[str, Dict[str, Any]] = self._load()
        self._log = log.bind(component="AICache")

    # ------------------------------------------------------------------ keys

    @staticmethod
    def _key(symbol: str, strategy: str) -> str:
        """Build the cache key for a symbol+strategy pair."""
        return f"{symbol.upper()}:{strategy.lower()}"

    # ------------------------------------------------------------------ get

    def get(self, symbol: str, strategy: str) -> Optional[Dict[str, Any]]:
        """Return a cached verdict if present and not expired.

        Args:
            symbol: Ticker symbol.
            strategy: Strategy name.

        Returns:
            The cached verdict dict (``{"decision", "reasoning", "cached_at"}``)
            with ``ai_cost_usd`` forced to 0.0, or ``None`` on miss/expiry.
        """
        entry = self._store.get(self._key(symbol, strategy))
        if entry is None:
            return None

        try:
            cached_at = datetime.fromisoformat(entry["cached_at"])
        except (KeyError, ValueError, TypeError):
            return None

        if datetime.now(tz=EASTERN) - cached_at > self.ttl:
            self._log.debug("ai_cache.expired", symbol=symbol, strategy=strategy)
            return None

        result = dict(entry)
        result["ai_cost_usd"] = 0.0  # cache hits are always free
        result["from_cache"] = True
        return result

    # ------------------------------------------------------------------ set

    def set(
        self,
        symbol: str,
        strategy: str,
        decision: str,
        reasoning: str,
    ) -> None:
        """Store a verdict and persist the cache to disk.

        Args:
            symbol: Ticker symbol.
            strategy: Strategy name.
            decision: ``"APPROVE"`` or ``"REJECT"``.
            reasoning: Free-text explanation from the AI layer.
        """
        self._store[self._key(symbol, strategy)] = {
            "decision": decision,
            "reasoning": reasoning,
            "cached_at": datetime.now(tz=EASTERN).isoformat(),
        }
        self._save()

    # ------------------------------------------------------------- internals

    def _load(self) -> Dict[str, Dict[str, Any]]:
        """Load the cache file, returning an empty dict on any problem."""
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self) -> None:
        """Persist the cache atomically."""
        try:
            fd, tmp = tempfile.mkstemp(
                dir=str(self._data_dir), prefix=".ai_cache_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self._store, f, indent=2, default=str)
                os.replace(tmp, str(self.path))
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            self._log.error("ai_cache.save_failed", error=str(exc))
