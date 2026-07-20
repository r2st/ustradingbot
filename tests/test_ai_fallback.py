"""
T5 — AI layer failure / fallback paths.

The analyst is *fail-closed*: any Tier-2 error (network, timeout, bad body,
unparseable verdict) must REJECT rather than approve, must not cache the error,
and must not crash the caller.  Complements ``tests/test_ai.py`` (which covers
the missing-key and veto-disabled paths).
"""

from __future__ import annotations

import httpx
import pytest

from ai.analyst import AIAnalyst, APPROVE, REJECT, _parse_verdict
from config.settings import Settings
from signals.signal_types import Grade, Signal


def _settings(**kw) -> Settings:
    base = dict(
        AI_VETO_ENABLED=True,
        OPENROUTER_API_KEY="test-key",
        AI_EARNINGS_BLACKOUT_DAYS=3,
    )
    base.update(kw)
    return Settings(**base)


def _signal() -> Signal:
    return Signal(
        symbol="AAPL",
        strategy="momentum",
        grade=Grade.A,
        signal_strength=0.9,
        entry_price=100.0,
        stop_price=95.0,
        target_price=115.0,
    )


@pytest.fixture(autouse=True)
def _no_earnings(monkeypatch):
    # Keep Tier 1 out of the way so we exercise Tier 2 fallback deterministically.
    monkeypatch.setattr("ai.analyst.is_earnings_within_days", lambda *a, **k: False)


async def test_network_error_fails_closed(monkeypatch):
    analyst = AIAnalyst(_settings())

    async def boom(_signal):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(analyst, "_call_openrouter", boom)
    decision = await analyst.evaluate(_signal())
    assert decision.decision == REJECT
    assert decision.tier == "error"
    # The error verdict must NOT be cached.
    assert analyst._cache.get("AAPL", "momentum") is None


async def test_timeout_fails_closed(monkeypatch):
    analyst = AIAnalyst(_settings())

    async def slow(_signal):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(analyst, "_call_openrouter", slow)
    decision = await analyst.evaluate(_signal())
    assert decision.decision == REJECT
    assert "fail" in decision.reasoning.lower()


async def test_missing_key_fails_closed():
    analyst = AIAnalyst(_settings(OPENROUTER_API_KEY=""))
    decision = await analyst.evaluate(_signal())
    assert decision.decision == REJECT
    assert decision.tier == "error"


async def test_veto_disabled_approves_without_calling_llm(monkeypatch):
    analyst = AIAnalyst(_settings(AI_VETO_ENABLED=False))

    async def must_not_call(_signal):
        raise AssertionError("LLM must not be called when veto is disabled")

    monkeypatch.setattr(analyst, "_call_openrouter", must_not_call)
    decision = await analyst.evaluate(_signal())
    assert decision.decision == APPROVE


# ---------------------------------------------------------------------------
# Verdict parsing is tolerant but fails closed on garbage.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "not json at all", "{", "null"])
def test_parse_verdict_garbage_rejects(text):
    # Garbage must never parse to APPROVE — the analyst fails closed.
    decision, _reason = _parse_verdict(text)
    assert decision == REJECT
