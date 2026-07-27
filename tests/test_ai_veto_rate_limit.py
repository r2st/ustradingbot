"""
The AI veto must not treat provider trouble as a risk verdict.

Before the LLM router, *any* Tier-2 exception failed closed.  A 429 from
OpenRouter's free tier therefore rejected every candidate for the rest of the
scan -- the bot stopped trading because a rate limiter said "later", not
because anything was wrong with the trades.

These tests pin the split:

* every provider rate-limited / 5xx / timed out  -> veto is SKIPPED (approve,
  ``tier="skipped"``, not cached);
* the model actually answering REJECT             -> honoured, as before;
* anything else (rejected key, bad request, empty output, no provider) -> still
  fails CLOSED.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ai import llm_router
from ai.analyst import APPROVE, REJECT, TIER_SKIPPED, AIAnalyst
from config.settings import Settings
from signals.signal_types import Grade, Signal


def _settings(data_dir, **kw) -> Settings:
    base = dict(
        DATA_DIR=data_dir,
        AI_VETO_ENABLED=True,
        OPENROUTER_API_KEY="or-key",
        AI_EARNINGS_BLACKOUT_DAYS=3,
    )
    base.update(kw)
    return Settings(**base)


def _signal(symbol: str = "AAPL") -> Signal:
    return Signal(
        symbol=symbol,
        strategy="momentum",
        grade=Grade.A,
        signal_strength=0.9,
        entry_price=100.0,
        stop_price=95.0,
        target_price=115.0,
    )


@pytest.fixture(autouse=True)
def _no_earnings(monkeypatch):
    """Keep Tier 1 out of the way so every test exercises Tier 2."""
    monkeypatch.setattr("ai.analyst.is_earnings_within_days", lambda *a, **k: False)


def _install(monkeypatch, handler):
    """Patch the router's HTTP client with *handler(url) -> Response | Exception*."""
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            result = handler(url)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(llm_router.httpx, "AsyncClient", _Client)


def _resp(url: str, status: int, *, body=None, headers=None) -> httpx.Response:
    return httpx.Response(
        status,
        request=httpx.Request("POST", url),
        headers=headers or {},
        json=body if body is not None else {"error": {"message": "boom"}},
    )


def _verdict(url: str, decision: str, reason: str = "because") -> httpx.Response:
    return _resp(
        url,
        200,
        body={
            "choices": [
                {
                    "message": {
                        "content": f'{{"decision": "{decision}", "reason": "{reason}"}}'
                    }
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        },
    )


# ---------------------------------------------------------------------------
# Transient provider trouble -> skip the veto (fail OPEN)
# ---------------------------------------------------------------------------


def test_429_everywhere_skips_the_veto(tmp_data_dir, monkeypatch):
    _install(monkeypatch, lambda url: _resp(url, 429, headers={"Retry-After": "60"}))
    analyst = AIAnalyst(_settings(tmp_data_dir))

    decision = asyncio.run(analyst.evaluate(_signal()))

    assert decision.decision == APPROVE
    assert decision.tier == TIER_SKIPPED
    assert decision.cost_usd == 0.0
    assert "unavailable" in decision.reasoning.lower()


def test_skipped_verdict_is_not_cached(tmp_data_dir, monkeypatch):
    """A skip is an absence of a verdict -- the next scan must re-ask."""
    _install(monkeypatch, lambda url: _resp(url, 429))
    analyst = AIAnalyst(_settings(tmp_data_dir))

    asyncio.run(analyst.evaluate(_signal()))
    assert analyst._cache.get("AAPL", "momentum") is None


def test_5xx_everywhere_skips_the_veto(tmp_data_dir, monkeypatch):
    _install(monkeypatch, lambda url: _resp(url, 503))
    analyst = AIAnalyst(_settings(tmp_data_dir))
    assert asyncio.run(analyst.evaluate(_signal())).tier == TIER_SKIPPED


def test_timeout_everywhere_skips_the_veto(tmp_data_dir, monkeypatch):
    _install(monkeypatch, lambda url: httpx.ReadTimeout("too slow"))
    analyst = AIAnalyst(_settings(tmp_data_dir))
    decision = asyncio.run(analyst.evaluate(_signal()))
    assert decision.decision == APPROVE
    assert decision.tier == TIER_SKIPPED


def test_connection_error_everywhere_skips_the_veto(tmp_data_dir, monkeypatch):
    _install(monkeypatch, lambda url: httpx.ConnectError("refused"))
    analyst = AIAnalyst(_settings(tmp_data_dir))
    assert asyncio.run(analyst.evaluate(_signal())).tier == TIER_SKIPPED


def test_fail_open_can_be_disabled(tmp_data_dir, monkeypatch):
    """Operators who want the old behaviour keep it with one flag."""
    _install(monkeypatch, lambda url: _resp(url, 429))
    analyst = AIAnalyst(
        _settings(tmp_data_dir, AI_FAIL_OPEN_ON_PROVIDER_ERROR=False)
    )
    decision = asyncio.run(analyst.evaluate(_signal()))
    assert decision.decision == REJECT
    assert decision.tier == "error"


# ---------------------------------------------------------------------------
# A 429 on one provider must not skip the veto -- the chain answers instead
# ---------------------------------------------------------------------------


def test_429_on_openrouter_falls_through_to_a_real_verdict(tmp_data_dir, monkeypatch):
    def handler(url):
        if "openrouter" in url:
            return _resp(url, 429, headers={"Retry-After": "60"})
        return _verdict(url, "REJECT", "guidance cut")

    _install(monkeypatch, handler)
    analyst = AIAnalyst(_settings(tmp_data_dir, GEMINI_API_KEY="gm-key"))

    decision = asyncio.run(analyst.evaluate(_signal()))
    assert decision.decision == REJECT
    assert decision.tier == "tier2"
    assert decision.provider == "gemini"


def test_fallback_verdict_costs_nothing(tmp_data_dir, monkeypatch):
    """Only OpenRouter has configured pricing; the fallbacks run free tiers."""
    def handler(url):
        if "openrouter" in url:
            return _resp(url, 429)
        return _verdict(url, "APPROVE")

    _install(monkeypatch, handler)
    settings = _settings(
        tmp_data_dir,
        GROQ_API_KEY="gq-key",
        OPENROUTER_INPUT_COST_PER_1M=1000.0,
        OPENROUTER_OUTPUT_COST_PER_1M=1000.0,
    )
    analyst = AIAnalyst(settings)

    decision = asyncio.run(analyst.evaluate(_signal()))
    assert decision.provider == "groq"
    assert decision.cost_usd == 0.0
    assert analyst.total_cost_usd == 0.0


def test_openrouter_verdict_still_costs(tmp_data_dir, monkeypatch):
    _install(monkeypatch, lambda url: _verdict(url, "APPROVE"))
    analyst = AIAnalyst(
        _settings(
            tmp_data_dir,
            OPENROUTER_INPUT_COST_PER_1M=1000.0,
            OPENROUTER_OUTPUT_COST_PER_1M=2000.0,
        )
    )
    decision = asyncio.run(analyst.evaluate(_signal()))
    # 100/1M * 1000 + 20/1M * 2000
    assert decision.cost_usd == pytest.approx(0.14)


# ---------------------------------------------------------------------------
# Real rejections and real errors are unchanged
# ---------------------------------------------------------------------------


def test_actual_reject_is_honoured(tmp_data_dir, monkeypatch):
    _install(monkeypatch, lambda url: _verdict(url, "REJECT", "fraud probe"))
    analyst = AIAnalyst(_settings(tmp_data_dir))

    decision = asyncio.run(analyst.evaluate(_signal()))
    assert decision.decision == REJECT
    assert decision.tier == "tier2"
    assert "fraud probe" in decision.reasoning
    # Real verdicts *are* cached.
    assert analyst._cache.get("AAPL", "momentum")["decision"] == REJECT


def test_actual_approve_is_honoured(tmp_data_dir, monkeypatch):
    _install(monkeypatch, lambda url: _verdict(url, "APPROVE", "clean"))
    analyst = AIAnalyst(_settings(tmp_data_dir))
    decision = asyncio.run(analyst.evaluate(_signal()))
    assert decision.approved
    assert decision.tier == "tier2"


@pytest.mark.parametrize("code", [401, 403])
def test_rejected_key_still_fails_closed(tmp_data_dir, monkeypatch, code):
    _install(monkeypatch, lambda url: _resp(url, code))
    analyst = AIAnalyst(_settings(tmp_data_dir))
    decision = asyncio.run(analyst.evaluate(_signal()))
    assert decision.decision == REJECT
    assert decision.tier == "error"


def test_bad_request_still_fails_closed(tmp_data_dir, monkeypatch):
    _install(monkeypatch, lambda url: _resp(url, 400))
    analyst = AIAnalyst(_settings(tmp_data_dir))
    assert asyncio.run(analyst.evaluate(_signal())).decision == REJECT


def test_empty_completion_still_fails_closed(tmp_data_dir, monkeypatch):
    _install(
        monkeypatch,
        lambda url: _resp(url, 200, body={"choices": [{"message": {"content": ""}}]}),
    )
    analyst = AIAnalyst(_settings(tmp_data_dir))
    decision = asyncio.run(analyst.evaluate(_signal()))
    assert decision.decision == REJECT
    assert decision.tier == "error"


def test_unparseable_verdict_still_fails_closed(tmp_data_dir, monkeypatch):
    _install(
        monkeypatch,
        lambda url: _resp(
            url, 200, body={"choices": [{"message": {"content": "maybe? unclear"}}]}
        ),
    )
    analyst = AIAnalyst(_settings(tmp_data_dir))
    decision = asyncio.run(analyst.evaluate(_signal()))
    assert decision.decision == REJECT
    # The call succeeded; the *verdict* was unusable, so this is tier2.
    assert decision.tier == "tier2"


def test_no_provider_configured_fails_closed(tmp_data_dir):
    analyst = AIAnalyst(
        _settings(
            tmp_data_dir, OPENROUTER_API_KEY="", GEMINI_API_KEY="", GROQ_API_KEY=""
        )
    )
    decision = asyncio.run(analyst.evaluate(_signal()))
    assert decision.decision == REJECT
    assert decision.tier == "error"
    assert "No LLM provider configured" in decision.reasoning


def test_gemini_key_alone_is_enough(tmp_data_dir, monkeypatch):
    """The veto runs off any configured provider, not OpenRouter specifically."""
    _install(monkeypatch, lambda url: _verdict(url, "APPROVE", "clean"))
    analyst = AIAnalyst(
        _settings(tmp_data_dir, OPENROUTER_API_KEY="", GEMINI_API_KEY="gm-key")
    )
    decision = asyncio.run(analyst.evaluate(_signal()))
    assert decision.approved
    assert decision.provider == "gemini"
