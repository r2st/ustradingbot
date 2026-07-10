"""
F1 — Learnings guard (the reader half of the Learnings Engine).

Before the engine places an entry, this guard consults the lessons the bot
wrote for itself (``learnings.jsonl``, produced by :mod:`ai.reflection`).  It
selects lessons that apply to the incoming signal — same strategy and
direction, with the signal's RSI/volume/grade inside the lesson's
``conditions`` — and turns the most restrictive applicable action into a
verdict:

* **avoid**           → REJECT the signal.
* **require_confirm** → allow only grade-A signals (demote everything else).
* **prefer**          → annotate; never blocks.

Only lessons that cleared the reflection engine's support and confidence gates
(stored with a binding ``action``) can reject or demote; ``observe`` notes are
advisory and ignored here.  The guard is pure and fail-open: any error reading
the store yields an ``allow`` verdict, so memory can never take down entries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import structlog

from journal.learnings import (
    AVOID,
    PREFER,
    REQUIRE_CONFIRM,
    Learning,
    LearningStore,
)

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

ALLOW = "allow"
DEMOTE = "demote"   # grade-A only
REJECT = "reject"


@dataclass
class LearningsVerdict:
    """Outcome of consulting the learnings for a signal.

    Attributes:
        action: ``"allow"``, ``"demote"`` (grade-A only), or ``"reject"``.
        reason: Human-readable justification.
        matched: The lessons that applied to the signal.
        prefer_notes: Lesson texts flagged ``prefer`` (positive annotations).
    """

    action: str = ALLOW
    reason: str = "no applicable learnings"
    matched: List[Learning] = field(default_factory=list)
    prefer_notes: List[str] = field(default_factory=list)

    @property
    def rejects(self) -> bool:
        """``True`` when the signal should be skipped."""
        return self.action == REJECT

    @property
    def demotes(self) -> bool:
        """``True`` when the signal should be restricted to grade A."""
        return self.action == DEMOTE


def _grade_rank(grade: str) -> int:
    """Map a grade letter to a rank (A=3 … F=0); unknown → 0."""
    return {"A": 3, "B": 2, "C": 1, "F": 0}.get(str(grade).upper(), 0)


def _conditions_match(learning: Learning, *, rsi: float, volume_ratio: float, grade: str) -> bool:
    """Whether a signal satisfies a lesson's ``conditions``.

    A lesson with no conditions applies to any signal of its strategy.  Each
    present condition must hold for the lesson to apply.
    """
    cond = learning.conditions or {}
    if not cond:
        return True

    rsi_above = cond.get("rsi_above")
    if isinstance(rsi_above, (int, float)) and not (rsi > float(rsi_above)):
        return False
    rsi_below = cond.get("rsi_below")
    if isinstance(rsi_below, (int, float)) and not (rsi < float(rsi_below)):
        return False
    vol_below = cond.get("volume_ratio_below")
    if isinstance(vol_below, (int, float)) and not (volume_ratio < float(vol_below)):
        return False
    vol_above = cond.get("volume_ratio_above")
    if isinstance(vol_above, (int, float)) and not (volume_ratio > float(vol_above)):
        return False
    grade_max = cond.get("grade_max")
    if isinstance(grade_max, str) and grade_max.strip():
        # Lesson targets setups at or below grade_max quality.
        if _grade_rank(grade) > _grade_rank(grade_max):
            return False
    return True


class LearningsGuard:
    """Applies stored lessons to an incoming signal.

    Args:
        settings: Application settings (enable flag, max lessons weighed).
        store: The :class:`LearningStore` to read lessons from.
    """

    def __init__(self, settings, store: LearningStore) -> None:
        self._settings = settings
        self._store = store
        self._log = log.bind(component="LearningsGuard")

    def evaluate(self, signal, now: Optional[datetime] = None) -> LearningsVerdict:
        """Return the learnings verdict for *signal* (fail-open on error)."""
        if not self._settings.LEARNINGS_ENABLED:
            return LearningsVerdict(reason="learnings disabled")

        try:
            lessons = self._store.load(now=now)
        except Exception as exc:  # noqa: BLE001 -- fail open
            self._log.warning("learnings_guard.load_failed", error=str(exc))
            return LearningsVerdict(reason=f"load error ({exc}) — allow")

        if not lessons:
            return LearningsVerdict(reason="no learnings recorded")

        strategy = str(signal.strategy).lower()
        direction = str(getattr(signal, "direction", "long")).lower()
        rsi = float(signal.rsi_value)
        volume_ratio = float(signal.volume_ratio)
        grade = signal.grade.value

        applicable: List[Learning] = []
        for lrn in lessons:
            if str(lrn.strategy).lower() != strategy:
                continue
            if str(lrn.direction or "long").lower() != direction:
                continue
            if not _conditions_match(lrn, rsi=rsi, volume_ratio=volume_ratio, grade=grade):
                continue
            applicable.append(lrn)

        if not applicable:
            return LearningsVerdict(reason="no lessons match this setup")

        # Weigh the most confident lessons first, capped for prompt/UX parity.
        applicable.sort(key=lambda l: l.confidence, reverse=True)
        applicable = applicable[: int(self._settings.LEARNINGS_MAX_RELEVANT)]

        prefer_notes = [l.lesson_text for l in applicable if l.action == PREFER]

        # Most restrictive binding action wins.
        avoid = [l for l in applicable if l.action == AVOID]
        if avoid:
            top = max(avoid, key=lambda l: l.confidence)
            return LearningsVerdict(
                action=REJECT,
                reason=f"learning {top.id}: {top.lesson_text}",
                matched=applicable,
                prefer_notes=prefer_notes,
            )

        confirm = [l for l in applicable if l.action == REQUIRE_CONFIRM]
        if confirm and _grade_rank(grade) < _grade_rank("A"):
            top = max(confirm, key=lambda l: l.confidence)
            return LearningsVerdict(
                action=DEMOTE,
                reason=f"learning {top.id} requires grade A: {top.lesson_text}",
                matched=applicable,
                prefer_notes=prefer_notes,
            )

        return LearningsVerdict(
            action=ALLOW,
            reason=(
                f"{len(prefer_notes)} favourable lesson(s)"
                if prefer_notes
                else "matched lessons are non-binding"
            ),
            matched=applicable,
            prefer_notes=prefer_notes,
        )
