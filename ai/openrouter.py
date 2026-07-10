"""
Shared OpenRouter chat helper.

A thin async wrapper around the OpenRouter chat-completions endpoint used by
the memory layer (F1 reflection).  The AI veto layer (:mod:`ai.analyst`) keeps
its own bespoke call for backward compatibility; this helper exists so new
callers don't duplicate the httpx/headers/usage boilerplate.

The OpenRouter API key is read from ``settings.OPENROUTER_API_KEY`` (sourced
from the ``OPENROUTER_API_KEY`` environment variable); it is never hard-coded.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import httpx


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
    """Call OpenRouter chat-completions and return ``(content, usage)``.

    Args:
        settings: Application settings (API key, base URL, timeout).
        model: OpenRouter model id (e.g. ``"openai/gpt-oss-20b:free"``).
        system: System prompt.
        user: User prompt.
        temperature: Sampling temperature (default deterministic 0.0).
        max_tokens: Completion token cap.
        timeout: Per-call timeout in seconds; defaults to
            ``settings.OPENROUTER_TIMEOUT_SECONDS``.

    Returns:
        A tuple of the assistant message text and the raw ``usage`` dict
        (empty dict when the provider omits usage).

    Raises:
        RuntimeError: If ``OPENROUTER_API_KEY`` is not configured.
        httpx.HTTPError: On network/HTTP failure (callers fail-open).
    """
    if not settings.OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not configured")

    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/r2st/USTradingBot",
        "X-Title": "USTradingBot",
    }

    async with httpx.AsyncClient(
        timeout=timeout or settings.OPENROUTER_TIMEOUT_SECONDS
    ) as client:
        resp = await client.post(
            f"{settings.OPENROUTER_BASE_URL}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

    content = (
        data.get("choices", [{}])[0].get("message", {}).get("content", "")
    ) or ""
    usage = data.get("usage", {}) or {}
    return content, usage
