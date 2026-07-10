"""
Third-party equity ratings (Feature 5).

Provides a *provider-agnostic* view of an equity's quant rating, analyst
consensus, and price target behind a small abstraction that mirrors
:class:`data.providers.MarketDataProvider`.  The default implementation is
**Finnhub** (official API, key already in the repo, clean license); an optional
**FMP** implementation is included.  Seeking Alpha is deliberately *not*
implemented — its ToS prohibit scraping — but the :class:`RatingsProvider`
Protocol is the documented extension point a user with a licensed SA feed can
implement.

The rating is normalized to a single ordered enum
(``STRONG_BUY > BUY > HOLD > SELL > STRONG_SELL``) so a "≥ Hold" entry filter is
provider-independent.  Lookups are TTL-cached (daily) and **fail-open**: any
error returns ``None`` so a ratings outage can never halt the scan.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum
from threading import RLock
from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class Rating(IntEnum):
    """Normalized quant rating, ordered so comparisons express quality."""

    STRONG_SELL = 0
    SELL = 1
    HOLD = 2
    BUY = 3
    STRONG_BUY = 4

    @classmethod
    def from_str(cls, value: str) -> Optional["Rating"]:
        """Parse ``"hold"`` / ``"strong_buy"`` (any case) to a :class:`Rating`."""
        key = str(value or "").strip().upper().replace(" ", "_").replace("-", "_")
        return cls.__members__.get(key)

    @property
    def label(self) -> str:
        return self.name.replace("_", " ").title()


def rating_from_score(score: float) -> Rating:
    """Map a 1..5 mean recommendation score to a :class:`Rating`.

    5 = all strong-buy, 1 = all strong-sell.  Banded around the integer levels.
    """
    if score >= 4.5:
        return Rating.STRONG_BUY
    if score >= 3.5:
        return Rating.BUY
    if score >= 2.5:
        return Rating.HOLD
    if score >= 1.5:
        return Rating.SELL
    return Rating.STRONG_SELL


@dataclass
class RatingSnapshot:
    """A point-in-time ratings view for one symbol."""

    symbol: str
    quant_rating: Optional[Rating]
    factor_grades: Dict[str, str] = field(default_factory=dict)
    consensus: Optional[str] = None
    price_target: Optional[float] = None
    recent_change: Optional[str] = None
    as_of: str = ""

    def upside_pct(self, current_price: Optional[float]) -> Optional[float]:
        """Return % upside to the price target from *current_price*."""
        if not self.price_target or not current_price or current_price <= 0:
            return None
        return round((self.price_target / current_price - 1.0) * 100.0, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quant_rating": self.quant_rating.label if self.quant_rating is not None else None,
            "quant_rating_rank": int(self.quant_rating) if self.quant_rating is not None else None,
            "factor_grades": dict(self.factor_grades),
            "consensus": self.consensus,
            "price_target": self.price_target,
            "recent_change": self.recent_change,
            "as_of": self.as_of,
        }


# ---------------------------------------------------------------------------
# Provider protocol + implementations
# ---------------------------------------------------------------------------


@runtime_checkable
class RatingsProvider(Protocol):
    """Interface every ratings backend implements."""

    name: str

    def get_rating(self, symbol: str) -> Optional[RatingSnapshot]:
        """Return a :class:`RatingSnapshot` for *symbol*, or ``None``."""
        ...


class FinnhubRatingsProvider:
    """Ratings from Finnhub's recommendation-trend + price-target endpoints."""

    name = "finnhub"

    def __init__(self, settings: Any, http_get: Optional[Callable[..., Any]] = None) -> None:
        self._settings = settings
        self._http_get = http_get  # injectable for tests
        self._log = log.bind(provider="finnhub")

    def get_rating(self, symbol: str) -> Optional[RatingSnapshot]:
        token = getattr(self._settings, "FINNHUB_API_KEY", "")
        if not token and self._http_get is None:
            return None
        symbol = symbol.upper()

        recs = self._fetch(
            "https://finnhub.io/api/v1/stock/recommendation",
            {"symbol": symbol, "token": token},
        )
        if not isinstance(recs, list) or not recs:
            return None
        latest = max(recs, key=lambda r: str(r.get("period", "")))
        prev = None
        ordered = sorted(recs, key=lambda r: str(r.get("period", "")))
        if len(ordered) >= 2:
            prev = ordered[-2]

        rating = _consensus_from_counts(latest)
        if rating is None:
            return None

        target = self._fetch(
            "https://finnhub.io/api/v1/stock/price-target",
            {"symbol": symbol, "token": token},
        )
        price_target = None
        if isinstance(target, dict):
            price_target = _as_float(target.get("targetMean") or target.get("targetMedian"))

        recent_change = _recent_change(latest, prev)

        return RatingSnapshot(
            symbol=symbol,
            quant_rating=rating,
            consensus=rating.label,
            price_target=price_target,
            recent_change=recent_change,
            as_of=str(latest.get("period", "") or ""),
        )

    def _fetch(self, url: str, params: dict) -> Any:
        if self._http_get is not None:
            return self._http_get(url, params)
        import httpx

        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()


class FMPRatingsProvider:
    """Ratings from Financial Modeling Prep's ``/rating`` endpoint (optional)."""

    name = "fmp"

    def __init__(self, settings: Any, http_get: Optional[Callable[..., Any]] = None) -> None:
        self._settings = settings
        self._http_get = http_get
        self._log = log.bind(provider="fmp")

    def get_rating(self, symbol: str) -> Optional[RatingSnapshot]:
        key = getattr(self._settings, "FMP_API_KEY", "")
        if not key and self._http_get is None:
            return None
        symbol = symbol.upper()
        data = self._fetch(
            f"https://financialmodelingprep.com/api/v3/rating/{symbol}",
            {"apikey": key},
        )
        row = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else None)
        if not isinstance(row, dict):
            return None
        rating = _fmp_rating(row.get("rating") or row.get("ratingRecommendation"))
        if rating is None:
            return None
        grades = {
            "DCF": str(row.get("ratingDetailsDCFRecommendation", "") or ""),
            "ROE": str(row.get("ratingDetailsROERecommendation", "") or ""),
            "PE": str(row.get("ratingDetailsPERecommendation", "") or ""),
        }
        grades = {k: v for k, v in grades.items() if v}
        return RatingSnapshot(
            symbol=symbol,
            quant_rating=rating,
            factor_grades=grades,
            consensus=rating.label,
            as_of=str(row.get("date", "") or ""),
        )

    def _fetch(self, url: str, params: dict) -> Any:
        if self._http_get is not None:
            return self._http_get(url, params)
        import httpx

        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()


def make_ratings_provider(
    settings: Any, http_get: Optional[Callable[..., Any]] = None
) -> RatingsProvider:
    """Return the ratings provider named by ``settings.RATINGS_PROVIDER``."""
    name = str(getattr(settings, "RATINGS_PROVIDER", "finnhub") or "finnhub").lower()
    if name == "fmp":
        return FMPRatingsProvider(settings, http_get)  # type: ignore[return-value]
    return FinnhubRatingsProvider(settings, http_get)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Cached, fail-open public accessor
# ---------------------------------------------------------------------------

_cache: Dict[str, tuple[float, Optional[RatingSnapshot]]] = {}
_cache_lock = RLock()


def get_rating_snapshot(
    symbol: str,
    settings: Any = None,
    provider: Optional[RatingsProvider] = None,
) -> Optional[RatingSnapshot]:
    """Return a TTL-cached :class:`RatingSnapshot`, or ``None`` (fail-open)."""
    symbol = str(symbol or "").upper()
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()

    ttl = float(getattr(settings, "RATINGS_CACHE_TTL_MINUTES", 720.0)) * 60.0
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(symbol)
        if entry is not None and now - entry[0] <= ttl:
            return entry[1]

    prov = provider or make_ratings_provider(settings)
    try:
        snap = prov.get_rating(symbol)
    except Exception as exc:  # noqa: BLE001 -- fail-open
        log.warning("ratings.fetch_failed", symbol=symbol, error=str(exc))
        snap = None

    with _cache_lock:
        _cache[symbol] = (now, snap)
    return snap


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consensus_from_counts(row: dict) -> Optional[Rating]:
    """Weighted mean of Finnhub recommendation counts -> a :class:`Rating`."""
    sb = _as_float(row.get("strongBuy")) or 0.0
    b = _as_float(row.get("buy")) or 0.0
    h = _as_float(row.get("hold")) or 0.0
    s = _as_float(row.get("sell")) or 0.0
    ss = _as_float(row.get("strongSell")) or 0.0
    total = sb + b + h + s + ss
    if total <= 0:
        return None
    score = (sb * 5 + b * 4 + h * 3 + s * 2 + ss * 1) / total
    return rating_from_score(score)


def _recent_change(latest: dict, prev: Optional[dict]) -> Optional[str]:
    """Describe a fresh consensus shift between the last two periods."""
    if not prev:
        return None
    new = _consensus_from_counts(latest)
    old = _consensus_from_counts(prev)
    if new is None or old is None or new == old:
        return None
    return f"upgrade to {new.label}" if new > old else f"downgrade to {new.label}"


def _fmp_rating(value: Any) -> Optional[Rating]:
    """Map FMP's letter/word rating to a normalized :class:`Rating`."""
    text = str(value or "").strip().upper()
    mapping = {
        "S": Rating.STRONG_BUY, "A": Rating.STRONG_BUY,
        "STRONG BUY": Rating.STRONG_BUY,
        "B": Rating.BUY, "BUY": Rating.BUY,
        "C": Rating.HOLD, "HOLD": Rating.HOLD, "NEUTRAL": Rating.HOLD,
        "D": Rating.SELL, "SELL": Rating.SELL,
        "F": Rating.STRONG_SELL, "STRONG SELL": Rating.STRONG_SELL,
    }
    return mapping.get(text)


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
