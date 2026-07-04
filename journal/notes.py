"""
Trade journal notes & tags (feature 3).

The CSV journal (:mod:`journal.trade_logger`) is append/update-in-place and has
a fixed 37-column schema, so free-form notes live *alongside* it in a JSON
sidecar (``DATA_DIR/trade_notes.json``) keyed by ``trade_id``.  Each entry holds
a free-text note and a list of tags, plus a last-updated timestamp.

Notes are searchable (substring over the note text) and filterable (by tag), so
the dashboard can surface "all trades tagged #mistake" or "notes mentioning
gap".  All writes are atomic (temp file + ``os.replace``).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Dict, List, Optional

_FILENAME = "trade_notes.json"
_TAG_RE = re.compile(r"[^a-z0-9_\-]+")


def normalize_tag(tag: str) -> str:
    """Lower-case a tag and strip everything but ``[a-z0-9_-]``."""
    return _TAG_RE.sub("", str(tag or "").strip().lower())


@dataclass
class TradeNote:
    """A note + tags attached to a single trade."""

    trade_id: str
    note: str = ""
    tags: List[str] = field(default_factory=list)
    updated_at: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "trade_id": self.trade_id,
            "note": self.note,
            "tags": list(self.tags),
            "updated_at": self.updated_at,
        }


class TradeNotesStore:
    """Thread-safe JSON store of per-trade notes and tags."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / _FILENAME
        self._lock = RLock()
        self._notes: Dict[str, TradeNote] = {}
        self._load()

    # ------------------------------------------------------------------ io

    def _load(self) -> None:
        with self._lock:
            if not self._path.exists():
                self._notes = {}
                return
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
            notes: Dict[str, TradeNote] = {}
            for tid, entry in (raw or {}).items():
                if not isinstance(entry, dict):
                    continue
                tags = [normalize_tag(t) for t in entry.get("tags", []) if normalize_tag(t)]
                notes[str(tid)] = TradeNote(
                    trade_id=str(tid),
                    note=str(entry.get("note", "")),
                    tags=sorted(dict.fromkeys(tags)),
                    updated_at=str(entry.get("updated_at", "")),
                )
            self._notes = notes

    def _save(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {tid: n.to_dict() for tid, n in self._notes.items()}, indent=2
        )
        fd, tmp = tempfile.mkstemp(dir=str(self._data_dir), prefix=".notes_", suffix=".tmp")
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

    # -------------------------------------------------------------- mutations

    def set_note(
        self,
        trade_id: str,
        note: Optional[str] = None,
        tags: Optional[List[str]] = None,
        now: Optional[datetime] = None,
    ) -> TradeNote:
        """Create or update the note/tags for *trade_id*.

        Passing ``note=None`` leaves the existing note text unchanged; passing
        ``tags=None`` leaves the existing tags unchanged.  Returns the stored
        :class:`TradeNote`.
        """
        tid = str(trade_id).strip()
        if not tid:
            raise ValueError("trade_id must not be empty")
        with self._lock:
            existing = self._notes.get(tid, TradeNote(trade_id=tid))
            if note is not None:
                existing.note = str(note)
            if tags is not None:
                cleaned = [normalize_tag(t) for t in tags if normalize_tag(t)]
                existing.tags = sorted(dict.fromkeys(cleaned))
            existing.updated_at = (now or datetime.now()).isoformat(timespec="seconds")
            self._notes[tid] = existing
            self._save()
            return existing

    def delete(self, trade_id: str) -> bool:
        with self._lock:
            if str(trade_id) in self._notes:
                del self._notes[str(trade_id)]
                self._save()
                return True
            return False

    # ---------------------------------------------------------------- queries

    def get(self, trade_id: str) -> Optional[TradeNote]:
        with self._lock:
            return self._notes.get(str(trade_id))

    def all_notes(self) -> Dict[str, TradeNote]:
        with self._lock:
            return dict(self._notes)

    def all_tags(self) -> List[str]:
        with self._lock:
            tags: set[str] = set()
            for n in self._notes.values():
                tags.update(n.tags)
            return sorted(tags)

    def search(
        self, query: str = "", tag: str = ""
    ) -> List[TradeNote]:
        """Return notes matching a substring *query* and/or an exact *tag*.

        Both filters are optional; when both are given they combine with AND.
        Results are sorted by ``updated_at`` descending (most recent first).
        """
        q = str(query or "").strip().lower()
        want_tag = normalize_tag(tag) if tag else ""
        with self._lock:
            out: List[TradeNote] = []
            for n in self._notes.values():
                if q and q not in n.note.lower():
                    continue
                if want_tag and want_tag not in n.tags:
                    continue
                out.append(n)
        out.sort(key=lambda n: n.updated_at, reverse=True)
        return out


# ---------------------------------------------------------------------------
# Process-wide accessor
# ---------------------------------------------------------------------------

_STORES: Dict[str, TradeNotesStore] = {}
_STORES_LOCK = RLock()


def get_notes_store(data_dir: str | Path) -> TradeNotesStore:
    """Return a per-``data_dir`` cached :class:`TradeNotesStore` singleton."""
    key = str(Path(data_dir).resolve())
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = TradeNotesStore(data_dir)
            _STORES[key] = store
        return store
