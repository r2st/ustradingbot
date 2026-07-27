"""
Shared chat helper (routed through the multi-provider LLM chain).

A thin async wrapper used by the memory layer (F1 reflection).  It keeps its
original ``(content, usage)`` signature so existing callers are untouched, but
the request itself now goes through :mod:`ai.llm_router`, which tries
OpenRouter first and falls back to Gemini and then Groq when a provider is
rate-limited, erroring, or timing out.

The name is historical: this module predates the fallback chain.  Callers that
need to know *which* provider answered (or need the transient/auth
classification on failure) should use :func:`ai.llm_router.complete` directly.

API keys are read from settings (``OPENROUTER_API_KEY``, ``GEMINI_API_KEY``,
``GROQ_API_KEY``); they are never hard-coded.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ai.llm_router import complete


async def chat(
    settings,
    *,
    model: str,
    system: str,
    user: str,
    temperature: float = 0.0,
    max_tokens: int = 400,
    timeout: Optional[float] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Run a chat completion through the provider chain; return ``(content, usage)``.

    Args:
        settings: Application settings (API keys, base URLs, models, timeout).
        model: Preferred OpenRouter model id (e.g. ``"openai/gpt-oss-20b:free"``).
            Fallback providers use their own configured model.
        system: System prompt.
        user: User prompt.
        temperature: Sampling temperature (default deterministic 0.0).
        max_tokens: Completion token cap.
        timeout: Per-provider timeout in seconds; defaults to
            ``settings.OPENROUTER_TIMEOUT_SECONDS``.

    Returns:
        A tuple of the assistant message text and the raw ``usage`` dict
        (empty dict when the provider omits usage).

    Raises:
        ai.llm_router.AllProvidersFailed: When no provider is configured or
            every provider failed (callers fail open).
    """
    completion = await complete(
        settings,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    return completion.text, completion.usage
