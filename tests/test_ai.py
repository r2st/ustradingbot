"""Tests for the AI veto layer: cache, verdict parsing, and fail-closed logic."""

from __future__ import annotations

import asyncio

import pytest

from ai.analyst import APPROVE, REJECT, AIAnalyst, _parse_verdict
from ai.cache import AICache
from config.settings import Settings
from signals.signal_types import Grade, Signal


def _sig(symbol: str = "AAPL", strategy: str = "momentum") -> Signal:
    return Signal(
        symbol=symbol, strategy=strategy,
        entry_price=100.0, stop_price=95.0, target_price=115.0,
        signal_strength=0.82, grade=Grade.A, rsi_value=60.0,
    )


# ---------------------------------------------------------------- cache

def test_cache_roundtrip(settings: Settings) -> None:
    cache = AICache(str(settings.DATA_DIR), ttl_hours=4.0)
    assert cache.get("AAPL", "momentum") is None
    cache.set("AAPL", "momentum", APPROVE, "looks good")
    hit = cache.get("AAPL", "momentum")
    assert hit is not None
    assert hit["decision"] == APPROVE
    assert hit["ai_cost_usd"] == 0.0  # cache hits are free


def test_cache_expiry(settings: Settings) -> None:
    cache = AICache(str(settings.DATA_DIR), ttl_hours=0.0)  # instantly stale
    cache.set("AAPL", "momentum", APPROVE, "x")
    assert cache.get("AAPL", "momentum") is None


def test_cache_key_isolced_by_strategy(settings: Settings) -> None:
    cache = AICache(str(settings.DATA_DIR), ttl_hours=4.0)
    cache.set("AAPL", "momentum", APPROVE, "m")
    cache.set("AAPL", "pead", REJECT, "p")
    assert cache.get("AAPL", "momentum")["decision"] == APPROVE
    assert cache.get("AAPL", "pead")["decision"] == REJECT


# ---------------------------------------------------------------- parsing

@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"decision": "APPROVE", "reason": "clean"}', APPROVE),
        ('{"decision":"REJECT","reason":"fraud probe"}', REJECT),
        ('Sure!\n```json\n{"decision": "APPROVE", "reason": "ok"}\n```', APPROVE),
        ("I would APPROVE this trade.", APPROVE),
        ("This should be REJECT due to lawsuit.", REJECT),
        ("", REJECT),           # empty -> fail closed
        ("maybe, unclear", REJECT),  # unparseable -> fail closed
    ],
)
def test_parse_verdict(text: str, expected: str) -> None:
    decision, _reason = _parse_verdict(text)
    assert decision == expected


# ---------------------------------------------------------------- analyst

def test_tier1_earnings_blackout_rejects(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr("ai.analyst.is_earnings_within_days", lambda s, days=14: True)
    analyst = AIAnalyst(settings)
    decision = asyncio.run(analyst.evaluate(_sig("AAPL", "momentum")))
    assert decision.decision == REJECT
    assert decision.tier == "tier1"


def test_pead_skips_tier1(settings: Settings, monkeypatch) -> None:
    # Even with earnings imminent, PEAD must not be tier-1 rejected.
    monkeypatch.setattr("ai.analyst.is_earnings_within_days", lambda s, days=14: True)
    s = Settings(DATA_DIR=settings.DATA_DIR, AI_VETO_ENABLED=False)
    analyst = AIAnalyst(s)
    decision = asyncio.run(analyst.evaluate(_sig("AAPL", "pead")))
    assert decision.decision == APPROVE
    assert decision.tier == "disabled"


def test_missing_key_fails_closed(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr("ai.analyst.is_earnings_within_days", lambda s, days=14: False)
    s = Settings(DATA_DIR=settings.DATA_DIR, OPENROUTER_API_KEY="",
                 AI_VETO_ENABLED=True)
    analyst = AIAnalyst(s)
    decision = asyncio.run(analyst.evaluate(_sig()))
    assert decision.decision == REJECT
    assert decision.tier == "error"


def test_veto_disabled_approves(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr("ai.analyst.is_earnings_within_days", lambda s, days=14: False)
    s = Settings(DATA_DIR=settings.DATA_DIR, AI_VETO_ENABLED=False)
    analyst = AIAnalyst(s)
    decision = asyncio.run(analyst.evaluate(_sig()))
    assert decision.approved
    assert decision.tier == "disabled"
