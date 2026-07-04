"""
AI veto layer -- two-tier signal evaluation powered by OpenRouter.

This is the only (optionally) paid gate in the entry pipeline.  It runs
*after* technical scoring and risk pre-checks but *before* order placement.
Its job is to catch what the technical indicators cannot see: recent negative
news, imminent earnings, post-earnings reversals, or a fundamental cause
behind a price drop.

Two tiers
---------
**Tier 1 -- Earnings blackout (free).**  Before any LLM call, the analyst
checks the earnings calendar.  For non-PEAD strategies, if earnings fall
within ``AI_EARNINGS_BLACKOUT_DAYS`` (default 14) the signal is rejected
immediately -- no tokens spent.  PEAD signals skip this filter (they are, by
definition, post-earnings plays).

**Tier 2 -- LLM news veto (OpenRouter).**  The analyst sends a strategy-aware
prompt to an OpenRouter chat model (default ``openai/gpt-oss-20b:free``) and
parses a strict JSON verdict.  Results are cached per ``symbol+strategy`` for
``AI_CACHE_TTL_HOURS`` so repeat scans of the same stock are free.

Fail-closed
-----------
Per the system invariants, the AI layer **defaults to REJECT** on any error
(missing key, timeout, empty response, invalid JSON).  It never defaults to
APPROVE.  The one exception is when ``AI_VETO_ENABLED`` is ``False``: the
Tier-2 call is skipped entirely and signals that clear Tier 1 are approved.

The OpenRouter API key is read from the ``OPENROUTER_API_KEY`` environment
variable via :class:`config.settings.Settings`; it is never hard-coded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx
import structlog

from config.settings import Settings
from data.earnings import is_earnings_within_days
from signals.signal_types import Signal

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

from ai.cache import AICache

APPROVE = "APPROVE"
REJECT = "REJECT"


@dataclass
class AIDecision:
    """Outcome of an AI evaluation.

    Attributes:
        decision: ``"APPROVE"`` or ``"REJECT"``.
        reasoning: Human-readable justification.
        cost_usd: Estimated API cost in USD (0.0 for free models / cache hits).
        tier: Which tier produced the verdict (``"tier1"``, ``"tier2"``,
            ``"cache"``, ``"disabled"``, or ``"error"``).
    """

    decision: str
    reasoning: str
    cost_usd: float = 0.0
    tier: str = "tier2"

    @property
    def approved(self) -> bool:
        """``True`` when the decision is APPROVE."""
        return self.decision == APPROVE


# ---------------------------------------------------------------------------
# Strategy-aware prompt construction
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a risk-analyst gatekeeper for an automated long-only US/Canadian "
    "equity trading bot. A technical model has already produced a bullish "
    "signal. Your ONLY job is to VETO the trade if there is a fundamental or "
    "news-based reason it is likely to fail. Be conservative but do not veto "
    "for normal market noise. Respond with STRICT JSON only, no prose, in the "
    'form {"decision": "APPROVE" | "REJECT", "reason": "<one sentence>"}.'
)


def _strategy_guidance(strategy: str) -> str:
    """Return strategy-specific veto guidance for the prompt."""
    s = strategy.lower()
    if s == "pead":
        return (
            "This is a POST-EARNINGS DRIFT trade. Specifically check for news "
            "that reverses the initial positive earnings reaction: guidance cut "
            "after the print, analyst downgrades, restated numbers, CFO/CEO "
            "departure, or an SEC inquiry. REJECT if the post-earnings thesis "
            "is broken; otherwise APPROVE."
        )
    if s == "mean_reversion":
        return (
            "This is a MEAN-REVERSION bounce on a stock that dropped sharply. "
            "Determine whether the drop has a fundamental cause (guidance cut, "
            "lost major customer, product recall, fraud, litigation) versus "
            "pure panic/market-wide selling. REJECT if fundamental; APPROVE "
            "only if it looks like a panic selloff likely to bounce."
        )
    # vcp_breakout / momentum / swing
    return (
        "Standard news check. Only REJECT for MAJOR negative news in roughly "
        "the past 48 hours: fraud, major lawsuit, guidance cut, regulatory "
        "action, FDA rejection, or a failed acquisition. Otherwise APPROVE."
    )


def _build_user_prompt(signal: Signal) -> str:
    """Build the per-signal user prompt with context for the model."""
    return (
        f"Symbol: {signal.symbol}\n"
        f"Strategy: {signal.strategy}\n"
        f"Technical grade: {signal.grade.value} "
        f"(score {signal.signal_strength:.2f})\n"
        f"Proposed entry: {signal.entry_price}, stop: {signal.stop_price}, "
        f"target: {signal.target_price}\n"
        f"RSI: {signal.rsi_value}\n\n"
        f"{_strategy_guidance(signal.strategy)}\n\n"
        "Based on your knowledge of this company and any recent material "
        "developments, should this long trade be APPROVED or REJECTED? "
        "Return strict JSON."
    )


class AIAnalyst:
    """OpenRouter-backed AI veto gate.

    Args:
        settings: Application settings (provides the OpenRouter key/model,
            cache TTL, and earnings blackout window).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache = AICache(
            str(settings.DATA_DIR), ttl_hours=settings.AI_CACHE_TTL_HOURS
        )
        self._log = log.bind(component="AIAnalyst")
        self._total_cost_usd: float = 0.0

    @property
    def total_cost_usd(self) -> float:
        """Cumulative AI spend this process (excludes cache hits)."""
        return round(self._total_cost_usd, 6)

    # ------------------------------------------------------------- evaluate

    async def evaluate(self, signal: Signal) -> AIDecision:
        """Run the two-tier evaluation for *signal*.

        Args:
            signal: The scored, risk-cleared candidate signal.

        Returns:
            An :class:`AIDecision`.  Fails closed (REJECT) on any error.
        """
        symbol = signal.symbol
        strategy = signal.strategy.lower()
        bound = self._log.bind(symbol=symbol, strategy=strategy)

        # ── Tier 1: earnings blackout (skipped for PEAD) ──────────────────
        if strategy != "pead":
            try:
                if is_earnings_within_days(
                    symbol, days=self._settings.AI_EARNINGS_BLACKOUT_DAYS
                ):
                    bound.info("ai.tier1_reject", reason="earnings_within_window")
                    return AIDecision(
                        REJECT,
                        f"Earnings within {self._settings.AI_EARNINGS_BLACKOUT_DAYS} "
                        "days -- blackout.",
                        0.0,
                        tier="tier1",
                    )
            except Exception:
                # Earnings calendar is best-effort; do not fail the whole
                # evaluation if it errors -- fall through to Tier 2.
                bound.warning("ai.tier1_error")

        # ── Cache check ───────────────────────────────────────────────────
        cached = self._cache.get(symbol, strategy)
        if cached is not None:
            bound.info("ai.cache_hit", decision=cached["decision"])
            return AIDecision(
                cached["decision"], cached["reasoning"], 0.0, tier="cache"
            )

        # ── Tier 2 disabled: approve everything that cleared Tier 1 ───────
        if not self._settings.AI_VETO_ENABLED:
            return AIDecision(
                APPROVE, "AI veto disabled -- auto-approved.", 0.0, tier="disabled"
            )

        # ── Tier 2: OpenRouter LLM call ───────────────────────────────────
        if not self._settings.OPENROUTER_API_KEY:
            bound.error("ai.no_api_key")
            return AIDecision(
                REJECT,
                "OPENROUTER_API_KEY not configured -- failing closed.",
                0.0,
                tier="error",
            )

        try:
            decision = await self._call_openrouter(signal)
        except Exception as exc:  # noqa: BLE001 -- fail closed on anything
            bound.warning("ai.tier2_error", error=str(exc))
            return AIDecision(
                REJECT, f"AI call failed ({exc}) -- failing closed.", 0.0,
                tier="error",
            )

        # Cache the verdict (never cache errors).
        self._cache.set(symbol, strategy, decision.decision, decision.reasoning)
        self._total_cost_usd += decision.cost_usd
        bound.info(
            "ai.tier2_decision",
            decision=decision.decision,
            cost_usd=decision.cost_usd,
        )
        return decision

    # ------------------------------------------------------- openrouter call

    async def _call_openrouter(self, signal: Signal) -> AIDecision:
        """Make the OpenRouter chat-completions call and parse the verdict."""
        payload: Dict[str, Any] = {
            "model": self._settings.OPENROUTER_MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(signal)},
            ],
            "temperature": 0.0,
            "max_tokens": 300,
        }
        headers = {
            "Authorization": f"Bearer {self._settings.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            # Optional attribution headers recommended by OpenRouter.
            "HTTP-Referer": "https://github.com/r2st/USTradingBot",
            "X-Title": "USTradingBot",
        }

        async with httpx.AsyncClient(
            timeout=self._settings.OPENROUTER_TIMEOUT_SECONDS
        ) as client:
            resp = await client.post(
                f"{self._settings.OPENROUTER_BASE_URL}/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

        content = (
            data.get("choices", [{}])[0].get("message", {}).get("content", "")
        ) or ""
        usage = data.get("usage", {}) or {}
        cost = self._estimate_cost(
            usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        )

        decision, reasoning = _parse_verdict(content)
        return AIDecision(decision, reasoning, cost, tier="tier2")

    def _estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Estimate USD cost from token usage (0.0 for free models)."""
        return round(
            prompt_tokens / 1_000_000 * self._settings.OPENROUTER_INPUT_COST_PER_1M
            + completion_tokens
            / 1_000_000
            * self._settings.OPENROUTER_OUTPUT_COST_PER_1M,
            6,
        )


# ---------------------------------------------------------------------------
# Verdict parsing -- tolerant of models that wrap JSON in prose / code fences
# ---------------------------------------------------------------------------

def _parse_verdict(content: str) -> tuple[str, str]:
    """Extract ``(decision, reasoning)`` from a model response.

    Fails closed: if no valid APPROVE/REJECT can be parsed, returns REJECT.

    Args:
        content: Raw model text.

    Returns:
        ``(decision, reasoning)`` where decision is APPROVE or REJECT.
    """
    text = content.strip()
    if not text:
        return REJECT, "Empty AI response -- failing closed."

    # Try to locate a JSON object anywhere in the text.
    obj: Optional[Dict[str, Any]] = None
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            obj = None

    if isinstance(obj, dict) and "decision" in obj:
        decision = str(obj.get("decision", "")).strip().upper()
        reason = str(obj.get("reason") or obj.get("reasoning") or "").strip()
        if decision in (APPROVE, REJECT):
            return decision, (reason or "(no reason provided)")

    # Fallback: keyword scan of the raw text.
    upper = text.upper()
    if "REJECT" in upper and "APPROVE" not in upper:
        return REJECT, text[:200]
    if "APPROVE" in upper and "REJECT" not in upper:
        return APPROVE, text[:200]

    return REJECT, "Unparseable AI response -- failing closed."
