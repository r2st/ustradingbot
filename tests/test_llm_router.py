"""
Tests for the multi-provider LLM chain (:mod:`ai.llm_router`).

Three things must hold, because the AI veto keys its fail-open/fail-closed
decision off them:

* the chain tries OpenRouter first and only moves on when a provider fails;
* failures are classified correctly -- 429/5xx/timeout are *transient*, 401/403
  are *auth*, everything else is permanent;
* the circuit breaker takes a dead provider out of rotation without letting a
  persistently-rejected key masquerade as a rate limit once it trips.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ai import llm_router
from ai.llm_router import (
    AllProvidersFailed,
    CircuitBreaker,
    LLMError,
    breaker,
    build_providers,
    complete,
    configured_providers,
    parse_retry_after,
)
from config.settings import Settings

MESSAGES = [{"role": "user", "content": "hi"}]


def _settings(**kw) -> Settings:
    base = dict(
        OPENROUTER_API_KEY="or-key",
        GEMINI_API_KEY="gm-key",
        GROQ_API_KEY="gq-key",
        LLM_BREAKER_THRESHOLD=3,
        LLM_BREAKER_COOLDOWN_SECONDS=300.0,
    )
    base.update(kw)
    return Settings(**base)


def _ok_body(text: str = "hello", **usage) -> dict:
    return {
        "choices": [{"message": {"content": text}}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _install_client(monkeypatch, handler):
    """Patch the router's httpx.AsyncClient; *handler(url, payload)* -> Response.

    Records every attempted URL on the returned list so tests can assert the
    order providers were tried in.
    """
    attempts: list[str] = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            attempts.append(url)
            result = handler(url, json or {})
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(llm_router.httpx, "AsyncClient", _Client)
    return attempts


def _response(url: str, status: int, *, body=None, headers=None) -> httpx.Response:
    return httpx.Response(
        status,
        request=httpx.Request("POST", url),
        headers=headers or {},
        json=body if body is not None else {"error": {"message": "boom"}},
    )


# ---------------------------------------------------------------------------
# Provider construction
# ---------------------------------------------------------------------------


def test_chain_order_is_openrouter_gemini_groq():
    assert configured_providers(_settings()) == ["openrouter", "gemini", "groq"]


def test_providers_without_a_key_are_skipped():
    assert configured_providers(_settings(GEMINI_API_KEY="")) == [
        "openrouter",
        "groq",
    ]
    assert configured_providers(
        _settings(OPENROUTER_API_KEY="", GROQ_API_KEY="")
    ) == ["gemini"]


def test_fallback_can_be_switched_off():
    assert configured_providers(_settings(LLM_FALLBACK_ENABLED=False)) == [
        "openrouter"
    ]


def test_gemini_model_default_has_free_quota():
    # gemini-2.0-flash has zero free-tier quota and 429s on the first call;
    # the -latest alias is the one that actually serves.
    assert _settings().GEMINI_MODEL == "gemini-flash-latest"


def test_provider_url_and_headers():
    provider = build_providers(_settings())[0]
    assert provider.url.endswith("/chat/completions")
    assert "//chat" not in provider.url  # base URL trailing slash handled
    headers = provider.headers()
    assert headers["Authorization"] == "Bearer or-key"
    assert headers["X-Title"] == "USTradingBot"


# ---------------------------------------------------------------------------
# Retry-After parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("30", 30.0),
        ("0", 0.0),
        ("-5", 0.0),          # past / negative clamps to "right now"
        ("2.5", 2.5),
        ("not-a-date", None),
    ],
)
def test_parse_retry_after_delta_forms(value, expected):
    assert parse_retry_after(value) == expected


def test_parse_retry_after_http_date():
    # 60s past the reference instant (Thu, 01 Jan 1970 00:01:00 GMT == t+60).
    got = parse_retry_after("Thu, 01 Jan 1970 00:01:00 GMT", now=0.0)
    assert got == 60.0


def test_parse_retry_after_clamps_to_max():
    assert parse_retry_after("999999", max_seconds=3600.0) == 3600.0


# ---------------------------------------------------------------------------
# The chain: success, fallback, exhaustion
# ---------------------------------------------------------------------------


def test_first_provider_success_short_circuits(monkeypatch):
    attempts = _install_client(
        monkeypatch, lambda url, payload: _response(url, 200, body=_ok_body("hi"))
    )
    result = asyncio.run(complete(_settings(), MESSAGES))
    assert result.text == "hi"
    assert result.provider == "openrouter"
    assert result.usage["prompt_tokens"] == 10
    assert len(attempts) == 1  # gemini/groq never touched


def test_429_falls_back_to_gemini(monkeypatch):
    def handler(url, payload):
        if "openrouter" in url:
            return _response(url, 429, headers={"Retry-After": "42"})
        return _response(url, 200, body=_ok_body("from gemini"))

    attempts = _install_client(monkeypatch, handler)
    result = asyncio.run(complete(_settings(), MESSAGES))
    assert result.text == "from gemini"
    assert result.provider == "gemini"
    assert len(attempts) == 2


def test_falls_all_the_way_through_to_groq(monkeypatch):
    def handler(url, payload):
        if "groq" in url:
            return _response(url, 200, body=_ok_body("from groq"))
        return _response(url, 500)

    attempts = _install_client(monkeypatch, handler)
    result = asyncio.run(complete(_settings(), MESSAGES))
    assert result.provider == "groq"
    assert len(attempts) == 3


def test_timeout_falls_back(monkeypatch):
    def handler(url, payload):
        if "openrouter" in url:
            return httpx.ReadTimeout("too slow")
        return _response(url, 200, body=_ok_body("second"))

    _install_client(monkeypatch, handler)
    assert asyncio.run(complete(_settings(), MESSAGES)).provider == "gemini"


def test_connection_error_falls_back(monkeypatch):
    def handler(url, payload):
        if "openrouter" in url:
            return httpx.ConnectError("refused")
        return _response(url, 200, body=_ok_body("second"))

    _install_client(monkeypatch, handler)
    assert asyncio.run(complete(_settings(), MESSAGES)).provider == "gemini"


def test_empty_completion_falls_back(monkeypatch):
    def handler(url, payload):
        if "openrouter" in url:
            return _response(url, 200, body={"choices": [{"message": {"content": ""}}]})
        return _response(url, 200, body=_ok_body("real text"))

    _install_client(monkeypatch, handler)
    assert asyncio.run(complete(_settings(), MESSAGES)).text == "real text"


def test_reasoning_field_is_used_when_content_is_null(monkeypatch):
    body = {"choices": [{"message": {"content": None, "reasoning": "  thought  "}}]}
    _install_client(monkeypatch, lambda url, payload: _response(url, 200, body=body))
    assert asyncio.run(complete(_settings(), MESSAGES)).text == "thought"


def test_gemini_gets_reasoning_headroom(monkeypatch):
    """Gemini charges thinking tokens against max_tokens and hides them.

    At the veto's 300-token cap that came back ``finish_reason: "length"`` with
    an empty message, so the fallback needs both the low reasoning setting and
    room above the caller's cap.
    """
    seen: dict = {}

    def handler(url, payload):
        if "openrouter" in url:
            seen["openrouter"] = payload
            return _response(url, 429)
        seen["gemini"] = payload
        return _response(url, 200, body=_ok_body())

    _install_client(monkeypatch, handler)
    asyncio.run(complete(_settings(GROQ_API_KEY=""), MESSAGES, max_tokens=300))

    assert seen["openrouter"]["max_tokens"] == 300  # caller's cap, untouched
    assert seen["gemini"]["max_tokens"] > 300
    assert seen["gemini"]["reasoning_effort"] == "low"


def test_model_override_applies_to_openrouter_only(monkeypatch):
    seen: dict[str, str] = {}

    def handler(url, payload):
        provider = "openrouter" if "openrouter" in url else (
            "gemini" if "googleapis" in url else "groq"
        )
        seen[provider] = payload["model"]
        if provider == "openrouter":
            return _response(url, 429)
        return _response(url, 200, body=_ok_body())

    _install_client(monkeypatch, handler)
    settings = _settings()
    asyncio.run(complete(settings, MESSAGES, model="custom/model:free"))
    assert seen["openrouter"] == "custom/model:free"
    # A model id is provider-specific -- the fallback uses its own.
    assert seen["gemini"] == settings.GEMINI_MODEL


# ---------------------------------------------------------------------------
# Exhaustion + error classification
# ---------------------------------------------------------------------------


def test_no_providers_configured_raises():
    settings = _settings(
        OPENROUTER_API_KEY="", GEMINI_API_KEY="", GROQ_API_KEY=""
    )
    with pytest.raises(AllProvidersFailed) as exc:
        asyncio.run(complete(settings, MESSAGES))
    assert exc.value.no_providers is True
    assert exc.value.transient is False


def test_all_429_is_transient_with_earliest_retry_after(monkeypatch):
    retry = {"openrouter": "90", "googleapis": "30", "groq": "120"}

    def handler(url, payload):
        key = next(k for k in retry if k in url)
        return _response(url, 429, headers={"Retry-After": retry[key]})

    _install_client(monkeypatch, handler)
    with pytest.raises(AllProvidersFailed) as exc:
        asyncio.run(complete(_settings(), MESSAGES))
    assert exc.value.transient is True
    assert exc.value.auth_failed is False
    # Earliest deadline wins -- that's when it's worth trying again.
    assert exc.value.retry_after == 30.0


def test_all_5xx_is_transient(monkeypatch):
    _install_client(monkeypatch, lambda url, payload: _response(url, 503))
    with pytest.raises(AllProvidersFailed) as exc:
        asyncio.run(complete(_settings(), MESSAGES))
    assert exc.value.transient is True


def test_all_timeouts_are_transient(monkeypatch):
    _install_client(monkeypatch, lambda url, payload: httpx.ReadTimeout("slow"))
    with pytest.raises(AllProvidersFailed) as exc:
        asyncio.run(complete(_settings(), MESSAGES))
    assert exc.value.transient is True


@pytest.mark.parametrize("code", [401, 403])
def test_auth_failure_is_not_transient(monkeypatch, code):
    _install_client(monkeypatch, lambda url, payload: _response(url, code))
    with pytest.raises(AllProvidersFailed) as exc:
        asyncio.run(complete(_settings(), MESSAGES))
    assert exc.value.auth_failed is True
    assert exc.value.transient is False


def test_bad_request_is_not_transient(monkeypatch):
    _install_client(monkeypatch, lambda url, payload: _response(url, 400))
    with pytest.raises(AllProvidersFailed) as exc:
        asyncio.run(complete(_settings(), MESSAGES))
    assert exc.value.transient is False
    assert exc.value.auth_failed is False


def test_empty_completions_everywhere_are_not_transient(monkeypatch):
    body = {"choices": [{"message": {"content": ""}}]}
    _install_client(monkeypatch, lambda url, payload: _response(url, 200, body=body))
    with pytest.raises(AllProvidersFailed) as exc:
        asyncio.run(complete(_settings(), MESSAGES))
    assert exc.value.transient is False


def test_mixed_transient_and_permanent_is_not_transient(monkeypatch):
    def handler(url, payload):
        return _response(url, 429 if "openrouter" in url else 400)

    _install_client(monkeypatch, handler)
    with pytest.raises(AllProvidersFailed) as exc:
        asyncio.run(complete(_settings(), MESSAGES))
    # One provider gave a real (non-transient) failure -> not "just busy".
    assert exc.value.transient is False


def test_200_with_error_body_is_classified_by_code(monkeypatch):
    """OpenRouter reports upstream 429s as a 200 with an error body."""
    body = {"error": {"message": "rate limited upstream", "code": 429}}
    _install_client(monkeypatch, lambda url, payload: _response(url, 200, body=body))
    with pytest.raises(AllProvidersFailed) as exc:
        asyncio.run(complete(_settings(), MESSAGES))
    assert exc.value.transient is True


def test_200_with_unknown_error_body_is_permanent(monkeypatch):
    body = {"error": {"message": "model does not exist"}}
    _install_client(monkeypatch, lambda url, payload: _response(url, 200, body=body))
    with pytest.raises(AllProvidersFailed) as exc:
        asyncio.run(complete(_settings(), MESSAGES))
    assert exc.value.transient is False


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


def test_breaker_opens_after_threshold_consecutive_failures():
    cb = CircuitBreaker()
    assert cb.record_failure("p", threshold=3, cooldown=10.0, now=0.0) is False
    assert cb.record_failure("p", threshold=3, cooldown=10.0, now=0.0) is False
    assert cb.record_failure("p", threshold=3, cooldown=10.0, now=0.0) is True
    assert cb.is_open("p", now=5.0) is True
    assert cb.is_open("p", now=11.0) is False  # cooled down


def test_breaker_success_resets_the_count():
    cb = CircuitBreaker()
    cb.record_failure("p", threshold=3, cooldown=10.0, now=0.0)
    cb.record_failure("p", threshold=3, cooldown=10.0, now=0.0)
    cb.record_success("p")
    # Count restarted, so two more failures must not trip it.
    assert cb.record_failure("p", threshold=3, cooldown=10.0, now=0.0) is False
    assert cb.record_failure("p", threshold=3, cooldown=10.0, now=0.0) is False


def test_breaker_snapshot_reports_state():
    cb = CircuitBreaker()
    cb.record_failure("p", threshold=5, cooldown=10.0)
    snap = cb.snapshot()
    assert snap["p"]["failures"] == 1.0


def test_open_breaker_skips_the_provider(monkeypatch):
    def handler(url, payload):
        if "openrouter" in url:
            raise AssertionError("openrouter must be skipped while its circuit is open")
        return _response(url, 200, body=_ok_body("gemini answered"))

    attempts = _install_client(monkeypatch, handler)
    breaker.record_failure("openrouter", threshold=1, cooldown=300.0)
    result = asyncio.run(complete(_settings(), MESSAGES))
    assert result.provider == "gemini"
    assert len(attempts) == 1


def test_skipped_provider_inherits_its_failure_classification(monkeypatch):
    """A tripped breaker must not turn a rejected key into a "rate limit".

    Otherwise the veto would start failing *open* on a misconfigured key the
    moment the circuit opened -- exactly the trade it is meant to gate.
    """
    _install_client(monkeypatch, lambda url, payload: _response(url, 401))
    settings = _settings(GEMINI_API_KEY="", GROQ_API_KEY="", LLM_BREAKER_THRESHOLD=1)

    with pytest.raises(AllProvidersFailed) as first:
        asyncio.run(complete(settings, MESSAGES))
    assert first.value.auth_failed is True
    assert first.value.transient is False

    # Second call: the breaker is open, so the provider is skipped, not tried.
    with pytest.raises(AllProvidersFailed) as second:
        asyncio.run(complete(settings, MESSAGES))
    assert second.value.transient is False
    assert second.value.auth_failed is True


def test_skipped_provider_after_rate_limits_stays_transient(monkeypatch):
    _install_client(monkeypatch, lambda url, payload: _response(url, 429))
    settings = _settings(GEMINI_API_KEY="", GROQ_API_KEY="", LLM_BREAKER_THRESHOLD=1)

    with pytest.raises(AllProvidersFailed):
        asyncio.run(complete(settings, MESSAGES))
    with pytest.raises(AllProvidersFailed) as second:
        asyncio.run(complete(settings, MESSAGES))
    assert second.value.transient is True


def test_llm_error_carries_classification():
    err = LLMError("nope", provider="groq", status=429, retry_after=12.0, transient=True)
    assert err.provider == "groq"
    assert err.status == 429
    assert err.retry_after == 12.0
    assert err.transient is True
    assert err.auth is False
