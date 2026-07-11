"""
AI Memory dashboard API + section data (Memory & Learning layer, F1 + F2).

Surfaces the bot's self-written memory to the operator:

* **Learnings** — every lesson in ``learnings.jsonl`` (:mod:`journal.learnings`),
  written by the reflection engine (:mod:`ai.reflection`) after a trade closes.
* **Reflections timeline** — the same lessons ordered newest-first and paired
  with the closed-trade context that produced them.
* **Guard decisions** — the block/demote verdicts the similar-setup guard
  (:mod:`analytics.setup_similarity`) and learnings guard
  (:mod:`analytics.learnings_guard`) actually applied, read back from the
  rejected-signal log (``rejected_signals.jsonl``).
* **Stats** — total lessons, active bindings by type, and the guard hit rate.

Everything here is read-only and fail-soft: a missing file reads as empty and a
malformed record is skipped, mirroring the stores it reads from.  The heavy
lifting lives in the store classes; this module only shapes their output for the
dashboard's ``/api/memory/*`` endpoints.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import structlog
from fastapi import APIRouter, Depends

from config.settings import EASTERN, get_settings
from dashboard.auth import require_auth
from journal.btst_logger import RejectedSignalLogger
from journal.learnings import (
    AVOID,
    OBSERVE,
    PREFER,
    REQUIRE_CONFIRM,
    Learning,
    LearningStore,
)

log = structlog.get_logger(__name__)

router = APIRouter(tags=["memory"])

# The gates whose rejections originate in the memory & learning layer.  Anything
# else in the rejected-signal log came from a different entry gate.
_MEMORY_GATES = {"similar_setup", "learnings_guard"}

# Human-readable labels for the four binding levels a lesson can carry.
_ACTION_LABELS = {
    AVOID: "avoid",
    REQUIRE_CONFIRM: "require_confirm",
    PREFER: "prefer",
    OBSERVE: "observe",
}


def _data_dir() -> Path:
    return Path(get_settings().DATA_DIR)


def _store() -> LearningStore:
    return LearningStore(_data_dir())


def _learning_row(lrn: Learning, now: datetime) -> Dict[str, Any]:
    """Shape one :class:`Learning` for the dashboard (list + reflection views)."""
    outcome = lrn.outcome or {}
    pnl = outcome.get("pnl_net")
    return {
        "id": lrn.id,
        "trade_id": lrn.trade_id,
        "created_at": lrn.created_at,
        "expires_at": lrn.expires_at,
        "expired": lrn.is_expired(now),
        "symbol": lrn.symbol,
        "strategy": lrn.strategy,
        "direction": lrn.direction,
        "grade": lrn.grade,
        "lesson_text": lrn.lesson_text,
        "pattern_tags": list(lrn.pattern_tags or []),
        "action": lrn.action,
        "binding": lrn.action != OBSERVE,
        "conditions": dict(lrn.conditions or {}),
        "confidence": round(float(lrn.confidence or 0.0), 3),
        "support_count": int(lrn.support_count or 0),
        "outcome": {
            "pnl_net": pnl,
            "r_multiple": outcome.get("r_multiple"),
            "exit_reason": outcome.get("exit_reason", ""),
            "hold_duration_hours": outcome.get("hold_duration_hours"),
            "result": (
                "won" if isinstance(pnl, (int, float)) and pnl > 0
                else "lost" if isinstance(pnl, (int, float)) and pnl < 0
                else "flat"
            ),
        },
        "entry_indicators": dict(lrn.entry_indicators or {}),
    }


def _sorted_learnings(now: datetime) -> List[Dict[str, Any]]:
    """All lessons (expired included), newest first, shaped for the UI."""
    rows = [_learning_row(l, now) for l in _store().load(include_expired=True)]
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows


def _classify_guard(gate: str, reason: str) -> str:
    """Infer the guard verdict (``block``/``demote``/``reject``) from its reason.

    The rejected-signal log stores the guard's free-text reason but not the
    enum verdict; recover it from the phrasing both guards use.  A demote only
    reaches the log when the signal wasn't grade A, so a logged demote is still
    a rejection — we label it distinctly so the operator can tell "skipped
    entirely" from "would have needed grade A".
    """
    low = (reason or "").lower()
    if "grade a" in low or "grade-a" in low:
        return "demote"
    if gate == "learnings_guard":
        return "reject"
    return "block"


def _guard_decisions(limit: int = 100) -> List[Dict[str, Any]]:
    """Recent memory-guard rejections, newest first."""
    logger = RejectedSignalLogger(str(_data_dir()))
    # Pull a wide window then filter to the memory gates so the caller's limit
    # applies to guard decisions, not to all rejections.
    records = logger.get_recent_rejections(n=2000)
    out: List[Dict[str, Any]] = []
    for rec in records:
        gate = str(rec.get("reason", ""))
        if gate not in _MEMORY_GATES:
            continue
        detail = str(rec.get("detail", ""))
        out.append(
            {
                "timestamp": rec.get("timestamp"),
                "gate": gate,
                "guard": (
                    "Similar-Setup" if gate == "similar_setup" else "Learnings"
                ),
                "decision": _classify_guard(gate, detail),
                "symbol": rec.get("symbol"),
                "strategy": rec.get("strategy"),
                "direction": rec.get("direction"),
                "grade": rec.get("grade"),
                "entry_price": rec.get("entry_price"),
                "rsi_value": rec.get("rsi_value"),
                "volume_ratio": rec.get("volume_ratio"),
                "signal_strength": rec.get("signal_strength"),
                "reason": detail,
            }
        )
    out.reverse()  # get_recent_rejections returns oldest→newest
    return out[:limit]


def _stats(learnings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Headline counts: totals, active bindings by type, guard hit rate."""
    active = [l for l in learnings if not l["expired"]]
    by_action: Dict[str, int] = {k: 0 for k in _ACTION_LABELS}
    for l in active:
        by_action[l["action"]] = by_action.get(l["action"], 0) + 1
    binding_active = sum(
        by_action.get(a, 0) for a in (AVOID, REQUIRE_CONFIRM, PREFER)
    )

    # Guard hit rate — what share of all logged entry rejections came from the
    # memory guards.  Read the full rejection log once for the denominator.
    logger = RejectedSignalLogger(str(_data_dir()))
    all_rej = logger.get_recent_rejections(n=5000)
    total_rej = len(all_rej)
    guard_rej = sum(1 for r in all_rej if str(r.get("reason")) in _MEMORY_GATES)
    by_gate: Dict[str, int] = {}
    for r in all_rej:
        g = str(r.get("reason"))
        if g in _MEMORY_GATES:
            by_gate[g] = by_gate.get(g, 0) + 1

    return {
        "total_learnings": len(learnings),
        "active_learnings": len(active),
        "expired_learnings": len(learnings) - len(active),
        "binding_active": binding_active,
        "bindings_by_type": by_action,
        "guard_rejections": guard_rej,
        "guard_rejections_by_gate": by_gate,
        "total_rejections": total_rej,
        "guard_hit_rate": round(guard_rej / total_rej, 4) if total_rej else 0.0,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/memory/learnings")
async def api_memory_learnings(_user: str = Depends(require_auth)) -> Dict[str, Any]:
    """Every stored lesson (expired flagged), newest first."""
    now = datetime.now(tz=EASTERN)
    return {"learnings": _sorted_learnings(now)}


@router.get("/api/memory/reflections")
async def api_memory_reflections(_user: str = Depends(require_auth)) -> Dict[str, Any]:
    """Trade reflections timeline — lessons paired with the closing trade."""
    now = datetime.now(tz=EASTERN)
    return {"reflections": _sorted_learnings(now)}


@router.get("/api/memory/guard-decisions")
async def api_memory_guard_decisions(
    limit: int = 100, _user: str = Depends(require_auth)
) -> Dict[str, Any]:
    """Recent similar-setup / learnings guard block & demote decisions."""
    return {"decisions": _guard_decisions(limit=max(1, min(int(limit), 500)))}


@router.get("/api/memory/stats")
async def api_memory_stats(_user: str = Depends(require_auth)) -> Dict[str, Any]:
    """Headline memory-layer stats for the summary cards."""
    now = datetime.now(tz=EASTERN)
    return _stats(_sorted_learnings(now))


@router.get("/api/memory/overview")
async def api_memory_overview(_user: str = Depends(require_auth)) -> Dict[str, Any]:
    """One-shot payload powering the whole Memory section (single fetch)."""
    now = datetime.now(tz=EASTERN)
    learnings = _sorted_learnings(now)
    return {
        "generated_at": now.isoformat(),
        "stats": _stats(learnings),
        "learnings": learnings,
        "reflections": learnings,
        "guard_decisions": _guard_decisions(limit=100),
    }
