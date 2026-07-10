"""
Learnings store — the bot's plain-English memory (``learnings.jsonl``).

Part of F1 (AI Trade Reflection).  After a trade closes, the reflection engine
(:mod:`ai.reflection`) writes one structured lesson here; before new entries
the learnings guard (:mod:`analytics.learnings_guard`) reads them back.  This
module owns only the persistence: appending, loading, and pruning expired
lessons.  It is deliberately dependency-light (stdlib + structlog) so both the
engine and the dashboard can read the file cheaply.

Each line is one JSON object matching :class:`Learning`.  The file is
append-only in the hot path; pruning rewrites it atomically (tmp + rename).
Every operation is fail-soft: a malformed line is skipped, a missing file
reads as empty, and write errors are logged rather than raised.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from config.settings import EASTERN

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Valid guard actions, ordered from most to least restrictive.
AVOID = "avoid"
REQUIRE_CONFIRM = "require_confirm"
PREFER = "prefer"
OBSERVE = "observe"  # non-binding note (e.g. sample too small to act on)
_VALID_ACTIONS = {AVOID, REQUIRE_CONFIRM, PREFER, OBSERVE}


@dataclass
class Learning:
    """One lesson the bot wrote for itself after a trade closed.

    ``conditions`` is a free-form dict of the numeric bounds under which the
    lesson applies (e.g. ``{"rsi_above": 65, "volume_ratio_below": 2.0}``); the
    learnings guard matches an incoming signal against it.  ``support_count``
    records how many similar historical trades backed the lesson at creation
    time — the guard only *binds* on lessons with enough support.
    """

    id: str
    trade_id: str = ""
    created_at: str = ""
    expires_at: str = ""
    symbol: str = ""
    strategy: str = ""
    direction: str = "long"
    grade: str = ""
    outcome: Dict[str, Any] = field(default_factory=dict)
    entry_indicators: Dict[str, Any] = field(default_factory=dict)
    lesson_text: str = ""
    pattern_tags: List[str] = field(default_factory=list)
    action: str = OBSERVE
    conditions: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    support_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict for JSON output."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Learning":
        """Build a :class:`Learning` from a dict, ignoring unknown keys."""
        fields = cls.__dataclass_fields__  # type: ignore[attr-defined]
        clean = {k: v for k, v in data.items() if k in fields}
        return cls(**clean)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        """Whether this lesson has passed its ``expires_at`` timestamp."""
        if not self.expires_at:
            return False
        try:
            exp = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return False
        now = now or datetime.now(tz=EASTERN)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=EASTERN)
        return now >= exp


class LearningStore:
    """Append-only JSONL store of :class:`Learning` records.

    Args:
        data_dir: Directory holding ``learnings.jsonl`` (created if absent).
    """

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self.path: Path = self._data_dir / "learnings.jsonl"
        self._log = log.bind(component="LearningStore")

    # ----------------------------------------------------------------- write

    def append(self, learning: Learning) -> None:
        """Append one lesson as a JSON line.  Logs (never raises) on error."""
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(learning.to_dict(), ensure_ascii=False) + "\n")
            self._log.info(
                "learnings.appended",
                id=learning.id,
                symbol=learning.symbol,
                action=learning.action,
            )
        except OSError as exc:
            self._log.error("learnings.append_failed", error=str(exc))

    # ------------------------------------------------------------------ read

    def load(self, include_expired: bool = False, now: Optional[datetime] = None) -> List[Learning]:
        """Load all lessons.  Malformed lines are skipped; missing file → [].

        Args:
            include_expired: When ``False`` (default) lessons past their
                ``expires_at`` are filtered out.
            now: Reference time for expiry checks (defaults to now, ET).
        """
        if not self.path.exists():
            return []
        out: List[Learning] = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    lrn = Learning.from_dict(obj)
                    if not include_expired and lrn.is_expired(now):
                        continue
                    out.append(lrn)
        except OSError as exc:
            self._log.error("learnings.load_failed", error=str(exc))
            return []
        return out

    def count(self) -> int:
        """Total (unexpired) lessons currently stored."""
        return len(self.load())

    def next_id(self, now: Optional[datetime] = None) -> str:
        """Return a fresh ``lrn_YYYYMMDD_NNN`` id unique within the store."""
        now = now or datetime.now(tz=EASTERN)
        day = now.strftime("%Y%m%d")
        prefix = f"lrn_{day}_"
        existing = self.load(include_expired=True)
        n = sum(1 for lrn in existing if lrn.id.startswith(prefix)) + 1
        return f"{prefix}{n:03d}"

    # ----------------------------------------------------------------- prune

    def prune_expired(self, now: Optional[datetime] = None) -> int:
        """Rewrite the file without expired lessons.  Returns count removed."""
        if not self.path.exists():
            return 0
        kept = self.load(include_expired=False, now=now)
        all_records = self.load(include_expired=True, now=now)
        removed = len(all_records) - len(kept)
        if removed <= 0:
            return 0
        self._rewrite(kept)
        self._log.info("learnings.pruned", removed=removed, kept=len(kept))
        return removed

    def _rewrite(self, learnings: List[Learning]) -> None:
        """Atomically replace the file with *learnings* (tmp + rename)."""
        try:
            fd, tmp = tempfile.mkstemp(
                dir=str(self._data_dir), prefix=".learnings_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    for lrn in learnings:
                        f.write(json.dumps(lrn.to_dict(), ensure_ascii=False) + "\n")
                os.replace(tmp, str(self.path))
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            self._log.error("learnings.rewrite_failed", error=str(exc))


def default_expiry(created: datetime, max_age_days: int) -> str:
    """ISO timestamp *max_age_days* after *created*."""
    return (created + timedelta(days=int(max_age_days))).isoformat()
