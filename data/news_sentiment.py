"""
News sentiment filter (feature 4).

Before entering a trade the engine can consult recent company news and reject
the entry when the average sentiment is strongly negative.  News comes from
Finnhub's free ``company-news`` endpoint (https://finnhub.io); headlines are
scored with a small built-in financial lexicon so the filter works even when
Finnhub does not return its own sentiment field.

The whole thing is *fail-open*: any misconfiguration or network error results
in the trade being **approved**, so news filtering can never silently block all
trading.  Results are cached per symbol for ``NEWS_CACHE_TTL_MINUTES``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Callable, Dict, List, Optional

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ── Financial sentiment lexicon ──────────────────────────────────────────────
# Deliberately small and high-precision; each word contributes ±1 before the
# headline score is averaged and squashed into [-1, 1].
_POSITIVE = {
    "beat", "beats", "surge", "surges", "soar", "soars", "upgrade", "upgraded",
    "record", "growth", "rally", "rallies", "profit", "profits", "gains",
    "outperform", "buyback", "raised", "jumps", "strong", "wins", "approval",
    "breakthrough", "bullish", "tops", "rebound", "expands",
}
_NEGATIVE = {
    "miss", "misses", "plunge", "plunges", "downgrade", "downgraded", "lawsuit",
    "fraud", "bankruptcy", "probe", "recall", "cut", "cuts", "slump", "slumps",
    "halt", "halts", "warning", "warns", "weak", "loss", "losses", "decline",
    "declines", "investigation", "sinks", "tumble", "tumbles", "bearish",
    "layoffs", "default", "sec", "subpoena",
}


def score_headlines(headlines: List[str]) -> float:
    """Return the average sentiment of *headlines* in ``[-1, 1]``.

    Each headline scores ``(pos - neg) / (pos + neg)`` over its matched lexicon
    words (0 when it matches nothing); the return value averages the per-headline
    scores.  An empty or fully-neutral input yields ``0.0``.
    """
    if not headlines:
        return 0.0
    per_headline: List[float] = []
    for text in headlines:
        tokens = _tokenize(text)
        pos = sum(1 for t in tokens if t in _POSITIVE)
        neg = sum(1 for t in tokens if t in _NEGATIVE)
        if pos + neg == 0:
            per_headline.append(0.0)
        else:
            per_headline.append((pos - neg) / (pos + neg))
    return round(sum(per_headline) / len(per_headline), 4)


def _tokenize(text: str) -> List[str]:
    out: List[str] = []
    for raw in str(text or "").lower().split():
        word = "".join(ch for ch in raw if ch.isalpha())
        if word:
            out.append(word)
    return out


@dataclass
class SentimentResult:
    """Outcome of a per-symbol news sentiment check."""

    symbol: str
    approved: bool
    score: float
    article_count: int
    reason: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "symbol": self.symbol,
            "approved": self.approved,
            "score": self.score,
            "article_count": self.article_count,
            "reason": self.reason,
        }


class NewsSentimentFilter:
    """Reject trades whose recent news sentiment is strongly negative.

    Args:
        settings: Application settings (reads ``NEWS_*`` fields).
        fetcher: ``fetcher(symbol) -> list[dict]`` returning Finnhub-shaped
            article dicts (``headline``, ``summary``, ``datetime`` unix seconds,
            optional ``sentiment`` float).  Defaults to a Finnhub HTTP fetch.
        clock: injectable ``() -> float`` monotonic-ish seconds for cache TTL
            (tests pass a controllable clock).
    """

    def __init__(
        self,
        settings,
        fetcher: Optional[Callable[[str], List[dict]]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._default_fetcher = fetcher is None
        self._fetcher = fetcher or self._finnhub_fetch
        self._clock = clock
        self._cache: Dict[str, tuple[float, SentimentResult]] = {}
        self._lock = RLock()
        self._log = log.bind(component="NewsSentimentFilter")

    # ------------------------------------------------------------------ public

    def check(self, symbol: str) -> SentimentResult:
        """Return the (cached) sentiment verdict for *symbol*.  Never raises."""
        symbol = str(symbol or "").upper()
        s = self._settings
        if not getattr(s, "NEWS_SENTIMENT_ENABLED", False):
            return SentimentResult(symbol, True, 0.0, 0, "news filter disabled")
        if self._default_fetcher and not getattr(s, "FINNHUB_API_KEY", ""):
            return SentimentResult(symbol, True, 0.0, 0, "no Finnhub key (fail-open)")

        cached = self._get_cached(symbol)
        if cached is not None:
            return cached

        result = self._evaluate(symbol)
        self._put_cached(symbol, result)
        return result

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # ---------------------------------------------------------------- internal

    def _evaluate(self, symbol: str) -> SentimentResult:
        s = self._settings
        try:
            articles = self._fetcher(symbol) or []
        except Exception as exc:  # noqa: BLE001 -- fail-open on any fetch error
            self._log.warning("news.fetch_failed", symbol=symbol, error=str(exc))
            return SentimentResult(symbol, True, 0.0, 0, "news fetch failed (fail-open)")

        count = len(articles)
        if count < int(getattr(s, "NEWS_MIN_ARTICLES", 2)):
            return SentimentResult(
                symbol, True, 0.0, count, f"insufficient news ({count} articles)"
            )

        # Prefer Finnhub's own sentiment when present, else the lexicon.
        provided = [
            float(a["sentiment"])
            for a in articles
            if isinstance(a, dict) and a.get("sentiment") is not None
        ]
        if provided and len(provided) == count:
            score = round(sum(provided) / len(provided), 4)
        else:
            headlines = [
                f"{a.get('headline', '')} {a.get('summary', '')}"
                for a in articles
                if isinstance(a, dict)
            ]
            score = score_headlines(headlines)

        threshold = float(getattr(s, "NEWS_SENTIMENT_MIN_SCORE", -0.15))
        approved = score >= threshold
        reason = (
            f"sentiment {score:+.3f} >= {threshold:+.3f}"
            if approved
            else f"negative sentiment {score:+.3f} < {threshold:+.3f}"
        )
        return SentimentResult(symbol, approved, score, count, reason)

    def _get_cached(self, symbol: str) -> Optional[SentimentResult]:
        ttl = float(getattr(self._settings, "NEWS_CACHE_TTL_MINUTES", 30.0)) * 60.0
        with self._lock:
            entry = self._cache.get(symbol)
            if entry is None:
                return None
            ts, result = entry
            if self._clock() - ts > ttl:
                self._cache.pop(symbol, None)
                return None
            return result

    def _put_cached(self, symbol: str, result: SentimentResult) -> None:
        with self._lock:
            self._cache[symbol] = (self._clock(), result)

    def _finnhub_fetch(self, symbol: str) -> List[dict]:
        """Fetch recent company news from Finnhub (best-effort)."""
        import httpx

        s = self._settings
        lookback = int(getattr(s, "NEWS_LOOKBACK_DAYS", 3))
        today = datetime.now(timezone.utc).date()
        params = {
            "symbol": symbol,
            "from": (today - timedelta(days=lookback)).isoformat(),
            "to": today.isoformat(),
            "token": getattr(s, "FINNHUB_API_KEY", ""),
        }
        with httpx.Client(timeout=10.0) as client:
            resp = client.get("https://finnhub.io/api/v1/company-news", params=params)
            resp.raise_for_status()
            data = resp.json()
        return data if isinstance(data, list) else []
