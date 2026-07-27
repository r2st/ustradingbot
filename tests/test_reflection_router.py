"""
The reflection path (F1) really goes through the provider chain.

``ai.reflection`` calls :func:`ai.openrouter.chat`, which is now a thin shim
over :mod:`ai.llm_router`.  The existing learnings tests stub ``chat`` itself,
so these cover the layer underneath: the shim's ``(content, usage)`` contract,
its fallback behaviour, and reflection staying fail-open when the whole chain
is down (a missing lesson must never bubble into the trading loop).
"""

from __future__ import annotations

import asyncio

import httpx
import pandas as pd
import pytest

from ai import llm_router
from ai.llm_router import AllProvidersFailed
from ai.openrouter import chat
from ai.reflection import ReflectionEngine
from config.settings import Settings
from journal.learnings import LearningStore

LESSON = (
    '{"lesson_text": "Skip C-grade momentum after a gap.", '
    '"pattern_tags": ["gap"], "action": "observe", "confidence": 0.9}'
)


def _install(monkeypatch, handler):
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


def _resp(url, status, *, body=None, headers=None) -> httpx.Response:
    return httpx.Response(
        status,
        request=httpx.Request("POST", url),
        headers=headers or {},
        json=body if body is not None else {"error": {"message": "boom"}},
    )


def _content(url, text) -> httpx.Response:
    return _resp(
        url,
        200,
        body={
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        },
    )


def _settings(data_dir, **kw) -> Settings:
    base = dict(
        DATA_DIR=data_dir,
        OPENROUTER_API_KEY="or-key",
        LEARNINGS_ENABLED=True,
        LEARNINGS_MIN_TRADES_FOR_PATTERN=1,
    )
    base.update(kw)
    return Settings(**base)


def _closed_trade() -> dict:
    return {
        "trade_id": "T1", "symbol": "AAPL", "strategy": "momentum",
        "direction": "long", "grade": "B", "rsi_value": 61.0,
        "volume_ratio": 1.4, "macd_histogram": 0.2, "ema_score": 0.7,
        "entry_fill_price": 100.0, "stop_price": 95.0, "target_price": 110.0,
        "exit_reason": "stop_hit", "pnl_net": -80.0, "r_multiple": -1.0,
        "hold_duration_hours": 26.0,
    }


# ---------------------------------------------------------------- the shim


def test_chat_returns_content_and_usage(tmp_data_dir, monkeypatch):
    _install(monkeypatch, lambda url: _content(url, "a lesson"))
    content, usage = asyncio.run(
        chat(_settings(tmp_data_dir), model="m", system="s", user="u")
    )
    assert content == "a lesson"
    assert usage["prompt_tokens"] == 7


def test_chat_falls_back_on_429(tmp_data_dir, monkeypatch):
    def handler(url):
        if "openrouter" in url:
            return _resp(url, 429, headers={"Retry-After": "10"})
        return _content(url, "from the fallback")

    _install(monkeypatch, handler)
    content, _usage = asyncio.run(
        chat(
            _settings(tmp_data_dir, GROQ_API_KEY="gq-key"),
            model="m", system="s", user="u",
        )
    )
    assert content == "from the fallback"


def test_chat_raises_when_every_provider_fails(tmp_data_dir, monkeypatch):
    _install(monkeypatch, lambda url: _resp(url, 500))
    with pytest.raises(AllProvidersFailed):
        asyncio.run(chat(_settings(tmp_data_dir), model="m", system="s", user="u"))


# ------------------------------------------------------------- reflection


def test_reflection_writes_a_lesson_through_the_chain(tmp_data_dir, monkeypatch):
    def handler(url):
        if "openrouter" in url:
            return _resp(url, 429)
        return _content(url, LESSON)

    _install(monkeypatch, handler)
    settings = _settings(tmp_data_dir, GEMINI_API_KEY="gm-key")
    store = LearningStore(str(tmp_data_dir))
    engine = ReflectionEngine(settings, store)

    learning = asyncio.run(
        engine.reflect(_closed_trade(), all_trades=pd.DataFrame())
    )

    assert learning is not None
    assert "C-grade momentum" in learning.lesson_text


def test_reflection_fails_open_when_the_chain_is_exhausted(tmp_data_dir, monkeypatch):
    """No lesson is worse than a wrong lesson -- and must never raise."""
    _install(monkeypatch, lambda url: _resp(url, 429))
    store = LearningStore(str(tmp_data_dir))
    engine = ReflectionEngine(_settings(tmp_data_dir), store)

    learning = asyncio.run(
        engine.reflect(_closed_trade(), all_trades=pd.DataFrame())
    )
    assert learning is None
