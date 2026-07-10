"""
F1 — AI Trade Reflection (the writer half of the Learnings Engine).

When a position closes, :class:`ReflectionEngine` sends the trade's context to
an OpenRouter model and asks for one plain-English lesson: what happened, and
what the bot should do differently next time it sees a setup like this.  The
lesson is stored in ``learnings.jsonl`` (:mod:`journal.learnings`) and later
consulted by the learnings guard (:mod:`analytics.learnings_guard`).

Two guardrails keep hallucinated lessons from steering the bot:

* **Support gate** — a lesson only *binds* (``avoid`` / ``require_confirm`` /
  ``prefer``) when at least ``LEARNINGS_MIN_TRADES_FOR_PATTERN`` similar trades
  exist in the ledger.  A one-off loss is stored as a non-binding ``observe``
  note the operator can read, but the guard won't act on it.
* **Confidence gate** — the model must self-report confidence at or above
  ``LEARNINGS_MIN_CONFIDENCE`` for the lesson to bind.

Everything here is fail-open: a missing key, a timeout, or unparseable output
means no lesson is written and the trade simply goes unremarked.  Reflection
never influences the trade that triggered it — only future entries.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd
import structlog

from ai.openrouter import chat
from analytics.setup_similarity import match_similar_trades
from config.settings import EASTERN
from journal.learnings import (
    OBSERVE,
    _VALID_ACTIONS,
    Learning,
    LearningStore,
    default_expiry,
)

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_SYSTEM_PROMPT = (
    "You are the post-trade analyst for an automated equity trading bot. A "
    "trade has just closed. Write ONE concise, concrete lesson the bot can use "
    "the next time it sees a similar setup. Focus on what the technical context "
    "at entry (grade, RSI, volume, MACD) and the outcome imply about whether to "
    "take, tighten, or skip such setups. Do NOT invent facts you were not given. "
    "Respond with STRICT JSON only, no prose, of the form: "
    '{"lesson_text": "<1-3 sentences>", "pattern_tags": ["<tag>", ...], '
    '"action": "avoid" | "require_confirm" | "prefer" | "observe", '
    '"conditions": {"rsi_above": <num?>, "rsi_below": <num?>, '
    '"volume_ratio_below": <num?>, "volume_ratio_above": <num?>, '
    '"grade_max": "<A|B|C?>"}, "confidence": <0.0-1.0>}. '
    'Use "avoid" only for clearly bad setups, "prefer" for clearly good ones, '
    '"require_confirm" when the setup needs extra confirmation, and "observe" '
    "when it is inconclusive. Omit condition keys that do not apply."
)


class ReflectionEngine:
    """Writes one lesson per closed trade via OpenRouter.

    Args:
        settings: Application settings (model, thresholds, expiry).
        store: The :class:`LearningStore` lessons are appended to.
    """

    def __init__(self, settings, store: LearningStore) -> None:
        self._settings = settings
        self._store = store
        self._log = log.bind(component="ReflectionEngine")

    async def reflect(
        self,
        trade: Dict[str, Any],
        all_trades: Optional[pd.DataFrame] = None,
        now: Optional[datetime] = None,
    ) -> Optional[Learning]:
        """Produce and persist a lesson for one closed *trade*.

        Args:
            trade: A closed trade row as a dict (journal columns).
            all_trades: Optional pre-loaded completed-trades frame, used to
                count similar setups for the support gate.  Loaded on demand
                when ``None``.
            now: Reference time (defaults to now, ET).

        Returns:
            The stored :class:`Learning`, or ``None`` when disabled or on any
            error (fail-open).
        """
        if not self._settings.LEARNINGS_ENABLED:
            return None

        now = now or datetime.now(tz=EASTERN)
        symbol = str(trade.get("symbol", "")).strip()
        bound = self._log.bind(symbol=symbol, trade_id=trade.get("trade_id"))

        try:
            support = self._support_count(trade, all_trades, now)
            content, _usage = await chat(
                self._settings,
                model=self._settings.LEARNINGS_MODEL,
                system=_SYSTEM_PROMPT,
                user=_build_reflection_prompt(trade, support),
                max_tokens=int(self._settings.LEARNINGS_MAX_TOKENS),
            )
        except Exception as exc:  # noqa: BLE001 -- fail open, never raise
            bound.warning("reflection.call_failed", error=str(exc))
            return None

        parsed = _parse_lesson(content)
        if parsed is None:
            bound.warning("reflection.unparseable", raw=content[:160])
            return None

        action = self._resolve_action(
            parsed["action"], parsed["confidence"], support
        )

        created = now
        learning = Learning(
            id=self._store.next_id(created),
            trade_id=str(trade.get("trade_id", "")),
            created_at=created.isoformat(),
            expires_at=default_expiry(created, self._settings.LEARNINGS_MAX_AGE_DAYS),
            symbol=symbol,
            strategy=str(trade.get("strategy", "")),
            direction=str(trade.get("direction", "long") or "long"),
            grade=str(trade.get("grade", "")),
            outcome={
                "pnl_net": _num(trade.get("pnl_net")),
                "r_multiple": _num(trade.get("r_multiple")),
                "exit_reason": str(trade.get("exit_reason", "")),
                "hold_duration_hours": _num(trade.get("hold_duration_hours")),
            },
            entry_indicators={
                "rsi": _num(trade.get("rsi_value")),
                "macd_histogram": _num(trade.get("macd_histogram")),
                "ema_score": _num(trade.get("ema_score")),
                "volume_ratio": _num(trade.get("volume_ratio")),
            },
            lesson_text=parsed["lesson_text"],
            pattern_tags=parsed["pattern_tags"],
            action=action,
            conditions=parsed["conditions"],
            confidence=parsed["confidence"],
            support_count=support,
        )
        self._store.append(learning)
        bound.info(
            "reflection.stored",
            id=learning.id,
            action=action,
            support=support,
            confidence=learning.confidence,
        )
        return learning

    # ----------------------------------------------------------- internals

    def _support_count(
        self,
        trade: Dict[str, Any],
        all_trades: Optional[pd.DataFrame],
        now: datetime,
    ) -> int:
        """Count ledger trades similar to *trade* (the pattern's support)."""
        try:
            if all_trades is None:
                from analytics.performance import load_completed_trades

                all_trades = load_completed_trades(self._store.path.parent / "trades.csv")
            if all_trades is None or all_trades.empty:
                return 0
            matches = match_similar_trades(
                all_trades,
                strategy=str(trade.get("strategy", "")),
                direction=str(trade.get("direction", "long") or "long"),
                grade=str(trade.get("grade", "")),
                rsi_value=_num(trade.get("rsi_value")),
                volume_ratio=_num(trade.get("volume_ratio")),
                settings=self._settings,
                now=now,
            )
            return int(len(matches))
        except Exception as exc:  # noqa: BLE001 -- best effort
            self._log.debug("reflection.support_count_failed", error=str(exc))
            return 0

    def _resolve_action(self, action: str, confidence: float, support: int) -> str:
        """Downgrade a binding action to ``observe`` when guardrails aren't met."""
        if action not in _VALID_ACTIONS:
            return OBSERVE
        if action == OBSERVE:
            return OBSERVE
        if support < int(self._settings.LEARNINGS_MIN_TRADES_FOR_PATTERN):
            return OBSERVE
        if confidence < float(self._settings.LEARNINGS_MIN_CONFIDENCE):
            return OBSERVE
        return action


# ---------------------------------------------------------------------------
# Prompt + parsing helpers
# ---------------------------------------------------------------------------

def _build_reflection_prompt(trade: Dict[str, Any], support: int) -> str:
    """Render the per-trade reflection prompt from a closed trade row."""
    pnl = _num(trade.get("pnl_net"))
    outcome = "WON" if pnl > 0 else "LOST" if pnl < 0 else "BREAK-EVEN"
    return (
        f"Symbol: {trade.get('symbol', '')}\n"
        f"Strategy: {trade.get('strategy', '')}\n"
        f"Direction: {trade.get('direction', 'long') or 'long'}\n"
        f"Technical grade at entry: {trade.get('grade', '')}\n"
        f"RSI at entry: {_num(trade.get('rsi_value'))}\n"
        f"Volume ratio at entry: {_num(trade.get('volume_ratio'))}\n"
        f"MACD histogram at entry: {_num(trade.get('macd_histogram'))}\n"
        f"EMA structure score at entry: {_num(trade.get('ema_score'))}\n"
        f"Entry price: {_num(trade.get('entry_fill_price'))}, "
        f"stop: {_num(trade.get('stop_price'))}, "
        f"target: {_num(trade.get('target_price'))}\n"
        f"Exit reason: {trade.get('exit_reason', '')}\n"
        f"Outcome: {outcome} "
        f"(net P&L {pnl:+.2f}, R-multiple {_num(trade.get('r_multiple')):+.2f}, "
        f"held {_num(trade.get('hold_duration_hours')):.1f}h)\n"
        f"Similar setups already in the ledger: {support}\n\n"
        "Write the lesson as strict JSON."
    )


def _parse_lesson(content: str) -> Optional[Dict[str, Any]]:
    """Extract a validated lesson dict from model output, or ``None``.

    Tolerant of prose/code-fence wrapping (locates the outermost JSON object).
    Returns ``None`` when no usable ``lesson_text`` can be parsed.
    """
    text = (content or "").strip()
    if not text:
        return None

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    lesson_text = str(obj.get("lesson_text") or "").strip()
    if not lesson_text:
        return None

    action = str(obj.get("action") or OBSERVE).strip().lower()

    tags_raw = obj.get("pattern_tags") or []
    if isinstance(tags_raw, list):
        tags = [str(t).strip() for t in tags_raw if str(t).strip()]
    else:
        tags = [str(tags_raw).strip()] if str(tags_raw).strip() else []

    conditions = obj.get("conditions") or {}
    if not isinstance(conditions, dict):
        conditions = {}
    # Keep only recognised, non-null condition keys with sane types.
    clean_conditions: Dict[str, Any] = {}
    for key in ("rsi_above", "rsi_below", "volume_ratio_below", "volume_ratio_above"):
        val = conditions.get(key)
        if isinstance(val, (int, float)):
            clean_conditions[key] = float(val)
    gmax = conditions.get("grade_max")
    if isinstance(gmax, str) and gmax.strip().upper() in {"A", "B", "C"}:
        clean_conditions["grade_max"] = gmax.strip().upper()

    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return {
        "lesson_text": lesson_text,
        "pattern_tags": tags,
        "action": action,
        "conditions": clean_conditions,
        "confidence": confidence,
    }


def _num(value: Any) -> float:
    """Coerce a journal cell to float; blanks/garbage → 0.0."""
    try:
        if value is None or value == "" or pd.isna(value):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0
