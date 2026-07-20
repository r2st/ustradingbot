"""
Fail-open behaviour of the AI commentary engine on an OpenRouter auth error
(audit B-7).

A 401/403 from OpenRouter (a rejected/expired key) must degrade gracefully —
the refresh returns template prose, flags ``auth_failed``, records ``last_error``,
counts the failed call in metrics, and never raises or 500s the page.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from config.settings import Settings
from dashboard import ai_commentary as ac
from dashboard.ai_commentary import CommentaryEngine


@pytest.fixture
def engine(settings: Settings, monkeypatch):
    s = Settings(DATA_DIR=settings.DATA_DIR, OPENROUTER_API_KEY="rejected-key",
                 AI_COMMENTARY_ENABLED=True)
    eng = CommentaryEngine(s)
    # Minimal, deterministic facts so a refresh reaches the LLM call.
    monkeypatch.setattr(ac, "build_position_facts", lambda s: [{
        "symbol": "NVDA", "strategy": "momentum", "entry_price": 100.0,
        "stop_price": 95.0, "target_price": 109.0, "current_price": 103.0,
        "indicators": None, "sentiment": None,
        "key_levels": {"support": [], "resistance": []},
    }])
    monkeypatch.setattr(ac, "build_watchlist_facts", lambda s: [])
    monkeypatch.setattr(ac, "build_market_facts", lambda s: {
        "spy": {}, "qqq": {}, "vix": {}, "sectors": [],
        "bias": {"label": "mixed", "note": ""},
    })
    return eng


def _mock_openrouter_status(monkeypatch, status_code: int):
    """Patch httpx.AsyncClient.post to raise an HTTPStatusError with *status*."""
    class _Resp:
        status_code = None

        def __init__(self, code):
            self.status_code = code
            self.request = httpx.Request("POST", "https://openrouter.ai")

        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp(status_code)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


@pytest.mark.parametrize("code", [401, 403])
def test_auth_error_degrades_to_template(engine, monkeypatch, code):
    from dashboard import metrics

    metrics.reset()
    _mock_openrouter_status(monkeypatch, code)

    payload = asyncio.run(engine.refresh(force=True))

    # Did not raise; template prose is served.
    assert payload["positions"][0]["source"] == "template"
    assert payload["positions"][0]["commentary"]
    # The rejected key is flagged so the UI can show a degraded state.
    assert engine._auth_failed is True
    status = engine.status()
    assert status["auth_failed"] is True
    assert status["last_error"]
    # The failed call is counted in metrics.
    assert "ustb_ai_llm_calls_total" in metrics.render()
    assert 'result="http_error"' in metrics.render()


def test_network_error_degrades_to_template(engine, monkeypatch):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    payload = asyncio.run(engine.refresh(force=True))
    assert payload["positions"][0]["source"] == "template"
    assert engine.status()["last_error"]
