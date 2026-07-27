"""
Commentary-path 429 handling: fall back, then back off.

The Analyst page issues up to three LLM calls per refresh (one per panel).
Without a backoff, a rate-limited provider was asked the same doomed question
three times per refresh and once more every interval, spending the daily call
budget on requests that could only 429.

Contract pinned here:

* a 429 on OpenRouter falls through to the next provider and the page still
  gets LLM prose;
* when *every* provider is throttled, one call is made, a ``Retry-After``-derived
  backoff is armed, and the remaining panels are skipped without spending budget;
* the page never empties -- template prose renders throughout -- and the
  degraded message stays plain-language (no raw provider errors);
* a later success clears the backoff.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ai import llm_router
from config.settings import Settings
from dashboard import ai_commentary as ac
from dashboard.ai_commentary import CommentaryEngine

POSITIONS = [{
    "symbol": "NVDA", "strategy": "momentum", "entry_price": 100.0,
    "stop_price": 95.0, "target_price": 109.0, "current_price": 103.0,
    "indicators": None, "sentiment": None,
    "key_levels": {"support": [], "resistance": []},
}]
WATCHLIST = [{
    "symbol": "AAPL", "status": "idle", "price": 200.0, "change_pct": 0.0,
    "readiness": 20, "signal": None, "last_rejection": None,
    "indicators": None, "sentiment": None,
    "key_levels": {"support": [], "resistance": []},
}]
MARKET = {
    "spy": {}, "qqq": {}, "vix": {}, "sectors": [],
    "bias": {"label": "mixed", "note": ""},
}


@pytest.fixture
def facts(monkeypatch):
    """Deterministic facts so a refresh reaches all three LLM calls."""
    monkeypatch.setattr(ac, "build_position_facts", lambda s: [dict(r) for r in POSITIONS])
    monkeypatch.setattr(ac, "build_watchlist_facts", lambda s: [dict(r) for r in WATCHLIST])
    monkeypatch.setattr(ac, "build_market_facts", lambda s: dict(MARKET))


def _engine(tmp_data_dir, **kw) -> CommentaryEngine:
    base = dict(
        DATA_DIR=tmp_data_dir,
        OPENROUTER_API_KEY="or-key",
        AI_COMMENTARY_ENABLED=True,
    )
    base.update(kw)
    return CommentaryEngine(Settings(**base))


def _install(monkeypatch, handler) -> list:
    """Patch the router's HTTP client; returns the list of attempted URLs."""
    attempts: list = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            attempts.append(url)
            result = handler(url)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(llm_router.httpx, "AsyncClient", _Client)
    return attempts


def _resp(url, status, *, body=None, headers=None) -> httpx.Response:
    return httpx.Response(
        status,
        request=httpx.Request("POST", url),
        headers=headers or {},
        json=body if body is not None else {"error": {"message": "rate limited"}},
    )


def _prose(url) -> httpx.Response:
    """A well-formed commentary response covering both panels + market."""
    content = (
        '{"positions": [{"symbol": "NVDA", "commentary": "holding up", '
        '"action": "hold"}], "watchlist": [{"symbol": "AAPL", '
        '"commentary": "coiling"}], "summary": "mixed tape"}'
    )
    return _resp(url, 200, body={"choices": [{"message": {"content": content}}]})


# ---------------------------------------------------------------------------
# Fallback keeps the page on LLM prose
# ---------------------------------------------------------------------------


def test_429_on_openrouter_falls_back_to_gemini(tmp_data_dir, facts, monkeypatch):
    def handler(url):
        if "openrouter" in url:
            return _resp(url, 429, headers={"Retry-After": "30"})
        return _prose(url)

    _install(monkeypatch, handler)
    engine = _engine(tmp_data_dir, GEMINI_API_KEY="gm-key")

    payload = asyncio.run(engine.refresh(force=True))

    assert payload["positions"][0]["source"] == "llm"
    assert payload["market"]["source"] == "llm"
    # A working fallback is not a rate-limited state.
    assert engine.rate_limited() is False
    assert payload["ai_state"] == "live"


# ---------------------------------------------------------------------------
# Everything throttled -> one call, then backoff
# ---------------------------------------------------------------------------


def test_all_providers_429_arms_backoff_and_skips_remaining_panels(
    tmp_data_dir, facts, monkeypatch
):
    attempts = _install(
        monkeypatch, lambda url: _resp(url, 429, headers={"Retry-After": "45"})
    )
    engine = _engine(tmp_data_dir)

    payload = asyncio.run(engine.refresh(force=True))

    # Exactly one HTTP attempt: panels 2 and 3 saw the backoff and skipped.
    assert len(attempts) == 1
    assert payload["budget"]["used"] == 1
    assert engine.rate_limited() is True
    # Retry-After is honoured (45s), not the 60s default.
    assert 40.0 < engine.backoff_remaining() <= 45.0


def test_page_still_renders_template_prose_when_throttled(
    tmp_data_dir, facts, monkeypatch
):
    _install(monkeypatch, lambda url: _resp(url, 429))
    engine = _engine(tmp_data_dir)

    payload = asyncio.run(engine.refresh(force=True))

    assert payload["positions"][0]["source"] == "template"
    assert payload["positions"][0]["commentary"]
    assert payload["watchlist"][0]["source"] == "template"
    assert payload["market"]["source"] == "template"


def test_missing_retry_after_uses_the_default(tmp_data_dir, facts, monkeypatch):
    _install(monkeypatch, lambda url: _resp(url, 429))
    engine = _engine(tmp_data_dir, LLM_DEFAULT_RETRY_AFTER_SECONDS=90.0)

    asyncio.run(engine.refresh(force=True))
    assert 85.0 < engine.backoff_remaining() <= 90.0


def test_retry_after_zero_still_backs_off(tmp_data_dir, facts, monkeypatch):
    """A provider answering "retry in 0s" is still throttling us."""
    _install(monkeypatch, lambda url: _resp(url, 429, headers={"Retry-After": "0"}))
    engine = _engine(tmp_data_dir)

    asyncio.run(engine.refresh(force=True))
    assert engine.rate_limited() is True


def test_backoff_survives_the_next_refresh(tmp_data_dir, facts, monkeypatch):
    attempts = _install(monkeypatch, lambda url: _resp(url, 429, headers={"Retry-After": "300"}))
    engine = _engine(tmp_data_dir)

    asyncio.run(engine.refresh(force=True))
    assert len(attempts) == 1
    # Second refresh while still inside the window: no further provider calls.
    asyncio.run(engine.refresh(force=True))
    assert len(attempts) == 1
    assert engine._budget()["used"] == 1


def test_expired_backoff_lets_calls_through(tmp_data_dir, facts, monkeypatch):
    state = {"throttle": True}

    def handler(url):
        if state["throttle"]:
            return _resp(url, 429, headers={"Retry-After": "300"})
        return _prose(url)

    attempts = _install(monkeypatch, handler)
    engine = _engine(tmp_data_dir)

    asyncio.run(engine.refresh(force=True))
    assert engine.rate_limited() is True

    # Provider recovers and the window elapses.
    state["throttle"] = False
    engine._llm_backoff_until = 0.0
    payload = asyncio.run(engine.refresh(force=True))

    assert len(attempts) > 1
    assert payload["positions"][0]["source"] == "llm"
    # A success clears the backoff outright.
    assert engine.rate_limited() is False


# ---------------------------------------------------------------------------
# The UI-facing state
# ---------------------------------------------------------------------------


def test_rate_limited_state_is_plain_language(tmp_data_dir, facts, monkeypatch):
    _install(monkeypatch, lambda url: _resp(url, 429, headers={"Retry-After": "45"}))
    engine = _engine(tmp_data_dir)

    payload = asyncio.run(engine.refresh(force=True))

    assert payload["ai_state"] == "degraded"
    msg = payload["ai_state_message"]
    assert "rate-limited" in msg
    # Never the raw provider error.
    assert "429" not in msg and "http" not in msg.lower()
    # The technical detail stays available for operators.
    status = engine.status()
    assert status["rate_limited"] is True
    assert status["retry_after_seconds"] > 0
    assert "rate-limited" in (status["last_error"] or "")


def test_rate_limit_is_counted_in_metrics(tmp_data_dir, facts, monkeypatch):
    from dashboard import metrics

    metrics.reset()
    _install(monkeypatch, lambda url: _resp(url, 429))
    engine = _engine(tmp_data_dir)

    asyncio.run(engine.refresh(force=True))

    rendered = metrics.render()
    assert 'result="rate_limited"' in rendered
    # The skipped panels are counted separately so a quiet page is visible.
    assert 'result="rate_limited_skip"' in rendered


def test_timeout_everywhere_also_backs_off(tmp_data_dir, facts, monkeypatch):
    _install(monkeypatch, lambda url: httpx.ReadTimeout("slow"))
    engine = _engine(tmp_data_dir)

    payload = asyncio.run(engine.refresh(force=True))
    assert engine.rate_limited() is True
    assert payload["positions"][0]["source"] == "template"


def test_auth_failure_does_not_arm_the_rate_limit_backoff(
    tmp_data_dir, facts, monkeypatch
):
    """A rejected key is a config problem, not a rate limit -- different state."""
    _install(monkeypatch, lambda url: _resp(url, 401))
    engine = _engine(tmp_data_dir)

    payload = asyncio.run(engine.refresh(force=True))

    assert engine._auth_failed is True
    assert engine.rate_limited() is False
    assert "API key" in payload["ai_state_message"]
