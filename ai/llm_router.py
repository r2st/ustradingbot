"""
Multi-provider LLM chain -- OpenRouter -> Gemini -> Groq.

Every LLM consumer in the bot (the AI veto in :mod:`ai.analyst`, trade
reflection in :mod:`ai.reflection`, and the dashboard's live commentary in
:mod:`dashboard.ai_commentary`) funnels through :func:`complete`.  The point is
that one flaky upstream must never turn into a trading decision: OpenRouter's
free tier 429s regularly, and before this module that 429 surfaced to the veto
as "AI call failed" -- which fails closed and silently blocked every trade for
the rest of the scan.

Three things make the chain work:

* **One dialect.**  All three providers speak OpenAI's ``/chat/completions``,
  so they differ only in base URL, key and model name.  A provider with no key
  configured is skipped rather than attempted-and-failed.
* **Error classification.**  Failures are split into *transient* (429, 5xx,
  timeout, connection error) and *permanent* (401/403 auth, 400/404 bad
  request, empty/unparseable completion).  Callers need the distinction:
  a rate limit is not a verdict, so the veto fails **open** on transient
  exhaustion and stays fail-closed on everything else.
* **A circuit breaker.**  After ``LLM_BREAKER_THRESHOLD`` consecutive failures
  a provider is skipped for ``LLM_BREAKER_COOLDOWN_SECONDS``.  Without it a
  dead upstream costs a full timeout on *every* call, and the chain's latency
  becomes the sum of everything broken ahead of the one that works.  The first
  success resets the count.

When every provider is exhausted the chain raises :class:`AllProvidersFailed`,
which carries ``transient`` / ``auth_failed`` / ``retry_after`` so each caller
can degrade the way *it* should: the veto skips itself, reflection writes no
lesson, and the dashboard renders template prose and backs off until
``retry_after``.

Breaker state is per-process and in-memory.  With several workers each learns
about a dead provider independently -- fine, since the cost of learning is one
timeout and shared state buys little for how rarely this fires.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Sequence

import httpx
import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Statuses that mean "the provider is having trouble, try again later" rather
# than "your request was wrong".  429 is the one that matters most in practice
# (free-tier rate limits); 408/409 and every 5xx ride along.
_TRANSIENT_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
_AUTH_STATUSES = frozenset({401, 403})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class LLMError(RuntimeError):
    """One provider attempt failed.

    Attributes:
        provider: Provider name that failed (``"openrouter"``, ...).
        status: HTTP status code when the failure was an HTTP response.
        retry_after: Seconds to wait, parsed from a ``Retry-After`` header.
        transient: ``True`` for rate limits / 5xx / timeouts / network errors.
        auth: ``True`` when the provider rejected the credential (401/403).
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status: Optional[int] = None,
        retry_after: Optional[float] = None,
        transient: bool = False,
        auth: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status
        self.retry_after = retry_after
        self.transient = transient
        self.auth = auth


class AllProvidersFailed(RuntimeError):
    """Every configured provider failed, or none was configured.

    Attributes:
        errors: The per-provider :class:`LLMError` list, in attempt order.
        transient: ``True`` when at least one provider was tried and *every*
            failure was transient -- i.e. nothing is wrong with the request,
            the providers are just busy.  Callers that must not be blocked by
            provider trouble (the AI veto) key their fail-open off this.
        auth_failed: ``True`` when any provider rejected its credential.
        retry_after: Earliest suggested retry delay in seconds, when any
            provider supplied one.
        no_providers: ``True`` when no provider had an API key at all.
    """

    def __init__(
        self,
        message: str,
        *,
        errors: Optional[List[LLMError]] = None,
        no_providers: bool = False,
    ) -> None:
        super().__init__(message)
        self.errors: List[LLMError] = list(errors or [])
        self.no_providers = no_providers
        self.transient = bool(self.errors) and all(e.transient for e in self.errors)
        self.auth_failed = any(e.auth for e in self.errors)
        waits = [e.retry_after for e in self.errors if e.retry_after is not None]
        self.retry_after: Optional[float] = min(waits) if waits else None


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Provider:
    """One OpenAI-compatible chat endpoint."""

    name: str
    api_key: str
    base_url: str
    model: str
    # OpenRouter wants attribution headers; nobody else needs extras.
    extra_headers: Dict[str, str] = field(default_factory=dict)
    # Provider-specific request-body knobs (see ``build_providers``).
    extra_body: Dict[str, Any] = field(default_factory=dict)
    # Extra completion tokens to grant this provider on top of the caller's
    # cap.  Reasoning models spend part of ``max_tokens`` on internal thinking
    # that never appears in ``content``, so a cap sized for the visible answer
    # comes back empty.
    token_headroom: int = 0

    @property
    def url(self) -> str:
        """Full chat-completions URL for this provider."""
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def headers(self) -> Dict[str, str]:
        """Auth + content headers for a request to this provider."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }


def build_providers(settings) -> List[Provider]:
    """Return the chain in priority order, skipping anything without a key.

    Read fresh on every call so a key rotation (or a test monkeypatching
    settings) takes effect without a module reload.

    Args:
        settings: Application settings.

    Returns:
        Configured providers, OpenRouter first.  When
        ``LLM_FALLBACK_ENABLED`` is ``False`` only OpenRouter is returned.
    """
    candidates = [
        Provider(
            name="openrouter",
            api_key=str(getattr(settings, "OPENROUTER_API_KEY", "") or ""),
            base_url=str(getattr(settings, "OPENROUTER_BASE_URL", "") or ""),
            model=str(getattr(settings, "OPENROUTER_MODEL", "") or ""),
            extra_headers={
                "HTTP-Referer": "https://github.com/r2st/USTradingBot",
                "X-Title": "USTradingBot",
            },
        ),
    ]
    if getattr(settings, "LLM_FALLBACK_ENABLED", True):
        candidates += [
            Provider(
                name="gemini",
                api_key=str(getattr(settings, "GEMINI_API_KEY", "") or ""),
                base_url=str(getattr(settings, "GEMINI_BASE_URL", "") or ""),
                model=str(getattr(settings, "GEMINI_MODEL", "") or ""),
                # Gemini Flash thinks before it answers, and those thinking
                # tokens are charged against ``max_tokens`` while never showing
                # up in ``content``: at the veto's 300-token cap the reply came
                # back ``finish_reason: "length"`` with an empty message.  The
                # lowest reasoning setting the compat endpoint accepts, plus
                # headroom, keeps short prompts answerable.  ("none" is
                # rejected with a 400 by this endpoint.)
                extra_body={"reasoning_effort": "low"},
                token_headroom=256,
            ),
            Provider(
                name="groq",
                api_key=str(getattr(settings, "GROQ_API_KEY", "") or ""),
                base_url=str(getattr(settings, "GROQ_BASE_URL", "") or ""),
                model=str(getattr(settings, "GROQ_MODEL", "") or ""),
            ),
        ]
    return [p for p in candidates if p.api_key and p.base_url and p.model]


def configured_providers(settings) -> List[str]:
    """Names of the providers that have a key, in the order they'd be tried."""
    return [p.name for p in build_providers(settings)]


# ---------------------------------------------------------------------------
# Retry-After
# ---------------------------------------------------------------------------

def parse_retry_after(
    value: Optional[str],
    *,
    max_seconds: float = 3600.0,
    now: Optional[float] = None,
) -> Optional[float]:
    """Parse a ``Retry-After`` header into a delay in seconds.

    Handles both RFC 7231 forms -- a delta in seconds (``"30"``) and an HTTP
    date (``"Wed, 21 Oct 2015 07:28:00 GMT"``).  Negative or past values clamp
    to ``0.0``; anything above *max_seconds* clamps down to it, so a provider
    advertising a multi-day cool-off cannot park a feature indefinitely.

    Args:
        value: Raw header value, or ``None`` when absent.
        max_seconds: Upper clamp for the returned delay.
        now: Unix timestamp used for date-form headers (defaults to now).

    Returns:
        Delay in seconds, or ``None`` when the header is missing/unparseable.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        reference = time.time() if now is None else now
        seconds = when.timestamp() - reference

    if seconds != seconds:  # NaN
        return None
    return max(0.0, min(float(seconds), float(max_seconds)))


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

@dataclass
class _BreakerState:
    failures: int = 0
    open_until: float = 0.0
    # Classification of the most recent failure.  A skipped (breaker-open)
    # provider inherits it so that "all providers skipped" keeps the same
    # transient/permanent meaning as the failures that opened them -- a
    # persistently rejected API key must not start looking like a rate limit
    # the moment the breaker trips.
    last_transient: bool = False
    last_auth: bool = False


class CircuitBreaker:
    """Per-provider consecutive-failure counter with a cool-down.

    Deliberately trips on *consecutive* failures: an upstream that fails one
    request in ten is degraded, not down, and tripping on a cumulative count
    would eventually take it out of rotation permanently.

    Thresholds are passed per call rather than held on the instance because
    settings are constructed per process/test, not at import time.
    """

    def __init__(self) -> None:
        self._state: Dict[str, _BreakerState] = {}
        self._lock = threading.Lock()

    def is_open(self, name: str, *, now: Optional[float] = None) -> bool:
        """Return ``True`` when *name* should be skipped right now."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            state = self._state.get(name)
            return state is not None and state.open_until > moment

    def last_failure_transient(self, name: str) -> bool:
        """Whether *name*'s most recent failure was a transient one."""
        with self._lock:
            state = self._state.get(name)
            return bool(state and state.last_transient)

    def last_failure_auth(self, name: str) -> bool:
        """Whether *name*'s most recent failure was a rejected credential."""
        with self._lock:
            state = self._state.get(name)
            return bool(state and state.last_auth)

    def record_failure(
        self,
        name: str,
        *,
        threshold: int,
        cooldown: float,
        transient: bool = False,
        auth: bool = False,
        now: Optional[float] = None,
    ) -> bool:
        """Count a failure; return ``True`` if this one tripped the breaker."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            state = self._state.setdefault(name, _BreakerState())
            state.failures += 1
            state.last_transient = transient
            state.last_auth = auth
            if threshold > 0 and state.failures >= threshold:
                state.open_until = moment + max(0.0, cooldown)
                state.failures = 0
                return True
            return False

    def record_success(self, name: str) -> None:
        """Clear a provider's failure count after a successful call."""
        with self._lock:
            self._state.pop(name, None)

    def reset(self) -> None:
        """Clear all state -- used by tests and after a config change."""
        with self._lock:
            self._state.clear()

    def snapshot(self) -> Dict[str, Dict[str, float]]:
        """Current state, for the health endpoint / debugging."""
        now = time.monotonic()
        with self._lock:
            return {
                name: {
                    "failures": float(state.failures),
                    "seconds_until_retry": max(0.0, state.open_until - now),
                }
                for name, state in self._state.items()
            }


breaker = CircuitBreaker()


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Completion:
    """A finished completion plus which provider actually served it."""

    text: str
    provider: str
    model: str
    usage: Dict[str, Any] = field(default_factory=dict)


def _classify_status(
    provider: str,
    status: int,
    retry_after: Optional[float],
    detail: str,
) -> LLMError:
    """Turn an HTTP status into a classified :class:`LLMError`."""
    return LLMError(
        f"{provider} returned HTTP {status}{detail}",
        provider=provider,
        status=status,
        retry_after=retry_after,
        transient=status in _TRANSIENT_STATUSES,
        auth=status in _AUTH_STATUSES,
    )


async def _call(
    provider: Provider,
    messages: Sequence[Dict[str, str]],
    *,
    model: Optional[str],
    temperature: float,
    max_tokens: int,
    timeout: float,
    max_retry_after: float,
) -> Completion:
    """One provider attempt.  Raises :class:`LLMError` on any failure."""
    payload: Dict[str, Any] = {
        # An explicit per-call model override only makes sense for the provider
        # it was written for; everyone else gets their own configured model.
        "model": model if (model and provider.name == "openrouter") else provider.model,
        "messages": list(messages),
        "temperature": temperature,
        "max_tokens": max_tokens + provider.token_headroom,
    }
    payload.update(provider.extra_body)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                provider.url, json=payload, headers=provider.headers()
            )
    except httpx.TimeoutException as exc:
        raise LLMError(
            f"{provider.name} timed out after {timeout}s",
            provider=provider.name,
            transient=True,
        ) from exc
    except httpx.HTTPError as exc:
        # Connection reset, DNS failure, proxy error: the request never got a
        # verdict, so it is worth trying the next provider.
        raise LLMError(
            f"{provider.name} request failed: {exc}",
            provider=provider.name,
            transient=True,
        ) from exc

    if resp.status_code >= 400:
        retry_after = parse_retry_after(
            resp.headers.get("Retry-After"), max_seconds=max_retry_after
        )
        raise _classify_status(provider.name, resp.status_code, retry_after, "")

    try:
        data = resp.json()
    except ValueError as exc:
        raise LLMError(
            f"{provider.name} returned non-JSON body",
            provider=provider.name,
            status=resp.status_code,
        ) from exc

    # OpenRouter (and Gemini's compat layer) report upstream errors as a 200
    # with an ``error`` body rather than a non-2xx status.  A rate limit that
    # arrives this way is still a rate limit.
    if isinstance(data, dict) and "choices" not in data:
        err = data.get("error") if isinstance(data.get("error"), dict) else {}
        detail = str((err or {}).get("message") or "no choices returned")
        code = (err or {}).get("code")
        try:
            status = int(code) if isinstance(code, (int, float, str)) else 0
        except ValueError:
            status = 0
        if status in _TRANSIENT_STATUSES or status in _AUTH_STATUSES:
            raise _classify_status(provider.name, status, None, f": {detail}")
        raise LLMError(
            f"{provider.name} request failed: {detail}",
            provider=provider.name,
        )

    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(
            f"{provider.name} returned an unexpected payload: {exc}",
            provider=provider.name,
        ) from exc

    # Some free reasoning models park the answer in ``reasoning`` and leave
    # ``content`` null.
    text = ""
    if isinstance(message, dict):
        text = message.get("content") or message.get("reasoning") or ""
    if not isinstance(text, str) or not text.strip():
        raise LLMError(
            f"{provider.name} returned an empty completion",
            provider=provider.name,
        )

    usage = data.get("usage") if isinstance(data, dict) else {}
    return Completion(
        text=text.strip(),
        provider=provider.name,
        model=str(payload["model"]),
        usage=usage if isinstance(usage, dict) else {},
    )


async def complete(
    settings,
    messages: Sequence[Dict[str, str]],
    *,
    model: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 400,
    timeout: Optional[float] = None,
) -> Completion:
    """Try each configured provider in order; return the first success.

    Args:
        settings: Application settings (keys, base URLs, models, breaker knobs).
        messages: OpenAI-style chat messages.
        model: Optional model override -- applied to OpenRouter only, since a
            model id is provider-specific.  Fallbacks use their own configured
            model.
        temperature: Sampling temperature.
        max_tokens: Completion token cap.
        timeout: Per-provider timeout in seconds; defaults to
            ``OPENROUTER_TIMEOUT_SECONDS``.

    Returns:
        The first successful :class:`Completion`.

    Raises:
        AllProvidersFailed: When no provider is configured or all of them
            fail.  Inspect ``transient`` / ``auth_failed`` / ``retry_after``
            to decide how to degrade.
    """
    providers = build_providers(settings)
    if not providers:
        raise AllProvidersFailed(
            "No LLM provider is configured -- set OPENROUTER_API_KEY, "
            "GEMINI_API_KEY or GROQ_API_KEY",
            no_providers=True,
        )

    call_timeout = float(
        timeout
        if timeout is not None
        else getattr(settings, "OPENROUTER_TIMEOUT_SECONDS", 45.0)
    )
    threshold = int(getattr(settings, "LLM_BREAKER_THRESHOLD", 3) or 3)
    cooldown = float(getattr(settings, "LLM_BREAKER_COOLDOWN_SECONDS", 300.0) or 0.0)
    max_retry_after = float(
        getattr(settings, "LLM_MAX_RETRY_AFTER_SECONDS", 3600.0) or 3600.0
    )

    errors: List[LLMError] = []
    for provider in providers:
        if breaker.is_open(provider.name):
            log.debug("llm.provider_skipped", provider=provider.name, reason="breaker_open")
            errors.append(
                LLMError(
                    f"{provider.name} skipped: circuit open",
                    provider=provider.name,
                    transient=breaker.last_failure_transient(provider.name),
                    auth=breaker.last_failure_auth(provider.name),
                )
            )
            continue

        started = time.monotonic()
        try:
            completion = await _call(
                provider,
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=call_timeout,
                max_retry_after=max_retry_after,
            )
        except LLMError as exc:
            tripped = breaker.record_failure(
                provider.name,
                threshold=threshold,
                cooldown=cooldown,
                transient=exc.transient,
                auth=exc.auth,
            )
            log.warning(
                "llm.provider_failed",
                provider=provider.name,
                status=exc.status,
                transient=exc.transient,
                retry_after=exc.retry_after,
                breaker_opened=tripped,
                error=str(exc),
            )
            errors.append(exc)
            continue

        breaker.record_success(provider.name)
        if provider.name != providers[0].name:
            log.info(
                "llm.served_by_fallback",
                provider=provider.name,
                model=completion.model,
                elapsed_s=round(time.monotonic() - started, 3),
            )
        return completion

    failure = AllProvidersFailed(
        "All LLM providers failed: " + "; ".join(str(e) for e in errors),
        errors=errors,
    )
    log.warning(
        "llm.all_providers_failed",
        transient=failure.transient,
        auth_failed=failure.auth_failed,
        retry_after=failure.retry_after,
        providers=[p.name for p in providers],
    )
    raise failure


__all__ = [
    "AllProvidersFailed",
    "CircuitBreaker",
    "Completion",
    "LLMError",
    "Provider",
    "breaker",
    "build_providers",
    "complete",
    "configured_providers",
    "parse_retry_after",
]
