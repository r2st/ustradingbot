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

from config.settings import EASTERN

_FILENAME = "trade_notes.json"
_TAG_RE = re.compile(r"[^a-z0-9_\-]+")

# Field caps (P6f) — keep the sidecar bounded and safe.
_MAX_TEXT = 2000       # per free-text field
_MAX_SETUP = 40        # setup_type label
_MAX_LIST = 30         # tags / mistake_tags per note
_RATING_RANGE = (1, 5)

_UNSET = object()  # sentinel so set_note can distinguish "omit" from "clear"


def normalize_tag(tag: str) -> str:
    """Lower-case a tag and strip everything but ``[a-z0-9_-]``."""
    return _TAG_RE.sub("", str(tag or "").strip().lower())


def _clean_text(value: object, cap: int = _MAX_TEXT) -> str:
    return str(value or "")[:cap]


def _clean_tag_list(values: object) -> List[str]:
    """Normalise, dedupe, and cap a list of tags/mistake tags."""
    if not isinstance(values, (list, tuple)):
        return []
    cleaned = [normalize_tag(v) for v in values if normalize_tag(v)]
    return sorted(dict.fromkeys(cleaned))[:_MAX_LIST]


def _clean_rating(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        r = int(value)
    except (ValueError, TypeError):
        return None
    lo, hi = _RATING_RANGE
    return r if lo <= r <= hi else None


@dataclass
class TradeNote:
    """A note, tags, and structured post-mortem fields for a single trade (P6f)."""

    trade_id: str
    note: str = ""
    tags: List[str] = field(default_factory=list)
    updated_at: str = ""
    # -- structured journaling depth (P6f) --
    setup_type: str = ""            # e.g. "breakout", "pullback", "reversal"
    mistake_tags: List[str] = field(default_factory=list)  # e.g. ["chased"]
    what_worked: str = ""
    what_went_wrong: str = ""
    lesson: str = ""
    rating: Optional[int] = None    # subjective 1-5 execution grade

    def to_dict(self) -> Dict[str, object]:
        return {
            "trade_id": self.trade_id,
            "note": self.note,
            "tags": list(self.tags),
            "updated_at": self.updated_at,
            "setup_type": self.setup_type,
            "mistake_tags": list(self.mistake_tags),
            "what_worked": self.what_worked,
            "what_went_wrong": self.what_went_wrong,
            "lesson": self.lesson,
            "rating": self.rating,
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
                # Old notes predate the structured fields — defaults apply so
                # they still load (backward compatible).
                notes[str(tid)] = TradeNote(
                    trade_id=str(tid),
                    note=str(entry.get("note", "")),
                    tags=_clean_tag_list(entry.get("tags", [])),
                    updated_at=str(entry.get("updated_at", "")),
                    setup_type=_clean_text(entry.get("setup_type", ""), _MAX_SETUP),
                    mistake_tags=_clean_tag_list(entry.get("mistake_tags", [])),
                    what_worked=_clean_text(entry.get("what_worked", "")),
                    what_went_wrong=_clean_text(entry.get("what_went_wrong", "")),
                    lesson=_clean_text(entry.get("lesson", "")),
                    rating=_clean_rating(entry.get("rating")),
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
        *,
        setup_type: Optional[str] = None,
        mistake_tags: Optional[List[str]] = None,
        what_worked: Optional[str] = None,
        what_went_wrong: Optional[str] = None,
        lesson: Optional[str] = None,
        rating: object = _UNSET,
    ) -> TradeNote:
        """Create or update the note, tags, and post-mortem for *trade_id*.

        Every field is optional; passing ``None`` (or omitting it) leaves the
        stored value unchanged.  ``rating`` uses a sentinel so an explicit
        ``rating=None`` clears the rating.  All text/list fields are length- and
        size-capped and tags are normalised.  Returns the stored
        :class:`TradeNote`.
        """
        tid = str(trade_id).strip()
        if not tid:
            raise ValueError("trade_id must not be empty")
        with self._lock:
            existing = self._notes.get(tid, TradeNote(trade_id=tid))
            if note is not None:
                existing.note = _clean_text(note)
            if tags is not None:
                existing.tags = _clean_tag_list(tags)
            if setup_type is not None:
                existing.setup_type = _clean_text(setup_type, _MAX_SETUP)
            if mistake_tags is not None:
                existing.mistake_tags = _clean_tag_list(mistake_tags)
            if what_worked is not None:
                existing.what_worked = _clean_text(what_worked)
            if what_went_wrong is not None:
                existing.what_went_wrong = _clean_text(what_went_wrong)
            if lesson is not None:
                existing.lesson = _clean_text(lesson)
            if rating is not _UNSET:
                existing.rating = _clean_rating(rating)
            existing.updated_at = (now or datetime.now(tz=EASTERN)).isoformat(timespec="seconds")
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

    def facets(self) -> Dict[str, List[str]]:
        """Distinct setup types, mistake tags, and general tags present (P6f).

        Powers the filter dropdowns in the journal UI.
        """
        with self._lock:
            setups: set[str] = set()
            mistakes: set[str] = set()
            tags: set[str] = set()
            for n in self._notes.values():
                if n.setup_type:
                    setups.add(n.setup_type)
                mistakes.update(n.mistake_tags)
                tags.update(n.tags)
        return {
            "setup_types": sorted(setups),
            "mistake_tags": sorted(mistakes),
            "tags": sorted(tags),
        }

    def search(
        self,
        query: str = "",
        tag: str = "",
        setup_type: str = "",
        mistake: str = "",
        min_rating: Optional[int] = None,
    ) -> List[TradeNote]:
        """Filter notes; all criteria are optional and combine with AND (P6f).

        * ``query`` — case-insensitive substring across every text field and the
          trade id.
        * ``tag`` / ``mistake`` — the note must carry that (normalised) tag.
        * ``setup_type`` — exact (case-insensitive) setup match.
        * ``min_rating`` — the note's ``rating`` must be >= this.

        Results are sorted by ``updated_at`` descending (most recent first).
        """
        q = str(query or "").strip().lower()
        want_tag = normalize_tag(tag) if tag else ""
        want_mistake = normalize_tag(mistake) if mistake else ""
        want_setup = str(setup_type or "").strip().lower()
        with self._lock:
            out: List[TradeNote] = []
            for n in self._notes.values():
                if q and q not in self._haystack(n):
                    continue
                if want_tag and want_tag not in n.tags:
                    continue
                if want_mistake and want_mistake not in n.mistake_tags:
                    continue
                if want_setup and n.setup_type.lower() != want_setup:
                    continue
                if min_rating is not None and (n.rating is None or n.rating < min_rating):
                    continue
                out.append(n)
        out.sort(key=lambda n: n.updated_at, reverse=True)
        return out

    @staticmethod
    def _haystack(n: TradeNote) -> str:
        return " ".join([
            n.trade_id, n.note, n.setup_type, n.what_worked,
            n.what_went_wrong, n.lesson,
            " ".join(n.tags), " ".join(n.mistake_tags),
        ]).lower()


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
