"""Plain-English read of one stock's technicals for the public ``/stock`` pages.

Two layers, so the page never depends on an LLM being up:

* :func:`technical_summary` — deterministic sentences built from the screener
  row.  Always present, exact, and the numbers on the page come from it.
* :func:`ai_summary` — a short Gemini-written take on the same row, called
  server-side only (the key never reaches a browser) and cached per symbol so
  anonymous traffic costs at most one call per symbol per :data:`_AI_TTL`.  Any
  failure — no key, quota, timeout, a malformed reply — returns ``None`` and the
  page simply shows the deterministic text.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

import httpx
import structlog

log = structlog.get_logger(__name__)

#: One AI summary per symbol per hour.  The inputs are daily-bar indicators
#: behind a 10-minute snapshot cache, so an hour loses nothing a reader needs.
_AI_TTL = 3600.0
#: A failed call is remembered for a shorter spell, so a Gemini outage does not
#: become one slow timeout per page view.
_AI_FAIL_TTL = 300.0
_AI_TIMEOUT = 12.0

_lock = threading.Lock()
# symbol -> (summary or None, monotonic_expiry)
_ai_cache: Dict[str, tuple] = {}


def clear_cache() -> None:
    with _lock:
        _ai_cache.clear()


def _signed(value: Optional[float]) -> str:
    return f"{value:+.2f}%" if value is not None else "n/a"


def technical_summary(row: Dict[str, Any]) -> List[str]:
    """Deterministic sentences describing one screener row."""
    sym = row["symbol"]
    out: List[str] = []
    change, month = row.get("change_pct"), row.get("month_pct")
    if change is not None:
        direction = "up" if change > 0 else "down" if change < 0 else "flat"
        out.append(f"{sym} is {direction} {abs(change):.2f}% on the day at ${row['price']:.2f}.")
    if month is not None:
        out.append(f"Over the past month it has moved {_signed(month)}.")
    rsi = row.get("rsi")
    if rsi is not None:
        if rsi < 30:
            zone = "oversold territory (below 30), where selling has been heavy"
        elif rsi > 70:
            zone = "overbought territory (above 70), where buying has been heavy"
        else:
            zone = "the neutral 30–70 range"
        out.append(f"Its 14-day RSI is {rsi:.1f}, in {zone}.")
    trend = row.get("above_sma50")
    if trend is True:
        out.append("The price is above its 50-day moving average, so the medium-term trend is up.")
    elif trend is False:
        out.append("The price is below its 50-day moving average, so the medium-term trend is down.")
    off_high = row.get("off_high_pct")
    if off_high is not None:
        if off_high >= -1:
            out.append("It is trading at or near its 6-month high.")
        else:
            out.append(f"It sits {abs(off_high):.1f}% below its 6-month high.")
    return out


def _prompt(row: Dict[str, Any]) -> str:
    facts = "\n".join(technical_summary(row))
    return (
        f"You are writing a neutral, educational note for a free stock page about "
        f"{row['symbol']} ({row.get('sector') or 'US equity'}).\n"
        f"Facts (use only these numbers, do not invent any):\n{facts}\n\n"
        "Write 2-3 short sentences in plain English for a beginner explaining what "
        "these technical readings suggest together and what a cautious trader might "
        "watch next. No buy/sell recommendation, no price targets, no markdown."
    )


def _call_gemini(settings, prompt: str) -> Optional[str]:
    key = str(getattr(settings, "GEMINI_API_KEY", "") or "").strip()
    if not key:
        return None
    base = str(getattr(settings, "GEMINI_BASE_URL", "") or "").rstrip("/")
    model = str(getattr(settings, "GEMINI_MODEL", "") or "gemini-flash-latest")
    resp = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            # Flash spends part of the budget thinking before it answers.
            "max_tokens": 800,
        },
        timeout=_AI_TIMEOUT,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    text = " ".join(str(text or "").split())
    return text or None


def cached_ai_summary(symbol: str) -> Optional[str]:
    """The cached summary for *symbol* if one is fresh — never calls Gemini.

    The HTML page uses this so its first byte never waits on an LLM; the page
    then asks the JSON endpoint, which does make the call, to fill the gap.
    """
    with _lock:
        hit = _ai_cache.get(symbol)
        if hit is not None and time.monotonic() < hit[1]:
            return hit[0]
    return None


def ai_summary(row: Dict[str, Any], settings) -> Optional[str]:
    """Gemini's short take on *row*, cached; ``None`` whenever it is unavailable."""
    sym = row["symbol"]
    now = time.monotonic()
    with _lock:
        hit = _ai_cache.get(sym)
        if hit is not None and now < hit[1]:
            return hit[0]
    try:
        text = _call_gemini(settings, _prompt(row))
    except Exception as exc:  # noqa: BLE001 -- the page must render without AI
        log.warning("stock_insight.ai_failed", symbol=sym, error=str(exc)[:200])
        text = None
    ttl = _AI_TTL if text else _AI_FAIL_TTL
    with _lock:
        _ai_cache[sym] = (text, time.monotonic() + ttl)
    return text
