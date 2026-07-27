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

**Tier 2 -- LLM news veto.**  The analyst sends a strategy-aware prompt through
:mod:`ai.llm_router` (OpenRouter, falling back to Gemini then Groq) and parses
a strict JSON verdict.  Results are cached per ``symbol+strategy`` for
``AI_CACHE_TTL_HOURS`` so repeat scans of the same stock are free.

Fail-closed, with one carve-out for provider trouble
----------------------------------------------------
Per the system invariants, the AI layer **defaults to REJECT** on any error it
can attribute to the *answer*: a missing key, an empty response, invalid JSON,
a 400/404, or a model that simply won't produce a verdict.  It never defaults
to APPROVE on those.

It does **not** fail closed when every provider is merely unavailable -- all of
them rate-limited (429), 5xx, or timed out.  A rate limit is not a risk
verdict, and treating it as one silently halted trading for the rest of a scan
whenever OpenRouter's free tier throttled us.  In that case the veto *skips
itself* (``tier="skipped"``), logs a warning, and lets the signal through on
the strength of the technical scoring and risk checks that already cleared it.
The skip is never cached, so the next scan re-asks.  Set
``AI_FAIL_OPEN_ON_PROVIDER_ERROR=False`` to restore the old fail-closed-on-
everything behaviour.

The two other exits: when ``AI_VETO_ENABLED`` is ``False`` the Tier-2 call is
skipped entirely and signals that clear Tier 1 are approved.

Provider API keys are read from the environment / ``keys/`` files via
:class:`config.settings.Settings`; they are never hard-coded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

import structlog

from ai.llm_router import AllProvidersFailed, complete, configured_providers
from config.settings import Settings
from data.earnings import is_earnings_within_days
from signals.signal_types import Signal

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

from ai.cache import AICache

APPROVE = "APPROVE"
REJECT = "REJECT"

# Tier label for "the providers were all unavailable, so no veto was applied".
# Distinct from ``"disabled"`` (operator turned the veto off) and ``"error"``
# (the veto ran and failed closed) so the journal can tell them apart.
TIER_SKIPPED = "skipped"


@dataclass
class AIDecision:
    """Outcome of an AI evaluation.

    Attributes:
        decision: ``"APPROVE"`` or ``"REJECT"``.
        reasoning: Human-readable justification.
        cost_usd: Estimated API cost in USD (0.0 for free models / cache hits).
        tier: Which tier produced the verdict (``"tier1"``, ``"tier2"``,
            ``"cache"``, ``"disabled"``, ``"skipped"``, or ``"error"``).
        provider: Which LLM provider served the verdict, when one did.
    """

    decision: str
    reasoning: str
    cost_usd: float = 0.0
    tier: str = "tier2"
    provider: str = ""

    @property
    def approved(self) -> bool:
        """``True`` when the decision is APPROVE."""
        return self.decision == APPROVE


# ---------------------------------------------------------------------------
# Strategy-aware prompt construction
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a risk-analyst gatekeeper for an automated US/Canadian equity "
    "trading bot that takes both long and short positions. A technical model "
    "has already produced a directional signal. Your ONLY job is to VETO the "
    "trade if there is a fundamental or news-based reason it is likely to "
    "fail. Be conservative but do not veto for normal market noise. Respond "
    "with STRICT JSON only, no prose, in the "
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
    if s.startswith("short_"):
        return (
            "This is a SHORT SALE betting on further downside. REJECT if "
            "there is a plausible upside catalyst that could squeeze the "
            "short: takeover/acquisition interest or rumours, activist "
            "involvement, a major pending positive announcement, heavy "
            "insider buying, or an obviously crowded short. Otherwise "
            "APPROVE."
        )
    if s == "hs_rsi2_reversal":
        return (
            "This is a HIGHLY SELECTIVE RSI-2 mean-reversion trade at a "
            "structural support/resistance level. REJECT if the stock has "
            "fundamental deterioration (guidance cut, lost customer, fraud) "
            "that makes the support level unreliable. APPROVE if the drop "
            "appears to be a panic selloff at a tested level."
        )
    if s == "hs_triple_timeframe":
        return (
            "This is a HIGHLY SELECTIVE triple-timeframe breakout requiring "
            "daily, 4H, and 1H alignment. REJECT if there is a major "
            "negative catalyst or a scheduled event (earnings, FDA, FOMC) "
            "that could invalidate the breakout. APPROVE otherwise."
        )
    if s == "hs_bb_climax":
        return (
            "This is a HIGHLY SELECTIVE Bollinger Band climax reversal "
            "targeting capitulation bottoms. REJECT if the selloff has a "
            "fundamental cause (fraud, major litigation, guidance cut) that "
            "makes a bounce unlikely. APPROVE if it looks like panic selling."
        )
    if s == "hs_pead_drift":
        return (
            "This is a HIGHLY SELECTIVE post-earnings drift trade. Check "
            "whether the earnings reaction is being reversed by negative "
            "follow-up news: guidance cut after the print, analyst "
            "downgrades, restated numbers, or executive departure. REJECT "
            "if the thesis is broken; APPROVE otherwise."
        )
    if s == "hs_gap_fill":
        return (
            "This is a HIGHLY SELECTIVE gap-fade trade betting the gap will "
            "fill. REJECT if the gap is driven by material news (earnings, "
            "M&A, FDA, upgrade/downgrade) that is unlikely to reverse "
            "intraday. APPROVE if the gap appears noise-driven."
        )
    if s == "hs_turnaround_tuesday":
        return (
            "This is a HIGHLY SELECTIVE Turnaround Tuesday calendar trade. "
            "REJECT if Monday's decline was driven by a major macro shock "
            "(rate hike, geopolitical crisis, systemic contagion) likely "
            "to persist through Tuesday. APPROVE for routine pullbacks."
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
        f"developments, should this {signal.direction} trade be APPROVED or "
        "REJECTED? Return strict JSON."
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

    @property
    def _fail_open_enabled(self) -> bool:
        """Whether an all-providers-unavailable chain skips the veto."""
        return bool(
            getattr(self._settings, "AI_FAIL_OPEN_ON_PROVIDER_ERROR", True)
        )

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

        # ── Tier 2: LLM call via the provider chain ───────────────────────
        providers = configured_providers(self._settings)
        if not providers:
            bound.error("ai.no_api_key")
            return AIDecision(
                REJECT,
                "No LLM provider configured (OPENROUTER_API_KEY / "
                "GEMINI_API_KEY / GROQ_API_KEY) -- failing closed.",
                0.0,
                tier="error",
            )

        try:
            decision = await self._call_llm(signal)
        except AllProvidersFailed as exc:
            # Provider trouble is not a risk verdict.  When *every* provider
            # was merely unavailable (429 / 5xx / timeout), skip the veto and
            # let the technical + risk gates stand; anything else (rejected
            # key, bad request, unusable output) still fails closed.
            if exc.transient and self._fail_open_enabled:
                bound.warning(
                    "ai.veto_skipped_provider_unavailable",
                    error=str(exc),
                    retry_after=exc.retry_after,
                    providers=providers,
                )
                return AIDecision(
                    APPROVE,
                    "AI providers temporarily unavailable (rate limit / "
                    "timeout) -- veto skipped, technical and risk gates "
                    "still applied.",
                    0.0,
                    tier=TIER_SKIPPED,
                )
            bound.warning("ai.tier2_error", error=str(exc), auth=exc.auth_failed)
            return AIDecision(
                REJECT, f"AI call failed ({exc}) -- failing closed.", 0.0,
                tier="error",
            )
        except Exception as exc:  # noqa: BLE001 -- fail closed on anything else
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
            provider=decision.provider,
        )
        return decision

    # ------------------------------------------------------------- LLM call

    async def _call_llm(self, signal: Signal) -> AIDecision:
        """Run the veto prompt through the provider chain and parse the verdict.

        Raises:
            AllProvidersFailed: When no provider produced usable output; the
                caller decides whether that fails open or closed.
        """
        completion = await complete(
            self._settings,
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(signal)},
            ],
            model=self._settings.OPENROUTER_MODEL,
            temperature=0.0,
            max_tokens=300,
        )
        usage = completion.usage or {}
        cost = self._estimate_cost(
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            provider=completion.provider,
        )

        decision, reasoning = _parse_verdict(completion.text)
        return AIDecision(
            decision, reasoning, cost, tier="tier2", provider=completion.provider
        )

    def _estimate_cost(
        self, prompt_tokens: int, completion_tokens: int, *, provider: str = "openrouter"
    ) -> float:
        """Estimate USD cost from token usage (0.0 for free models).

        Only OpenRouter has configured per-token pricing; the Gemini and Groq
        fallbacks are used on their free tiers, so a completion they served
        costs nothing to report.
        """
        if provider != "openrouter":
            return 0.0
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
