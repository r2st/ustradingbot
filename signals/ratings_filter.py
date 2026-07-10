"""
Third-party ratings entry filter (Feature 5).

Rejects a new entry when the symbol's normalized quant rating is below the
operator's configured minimum (default ``HOLD``).  Modeled directly on
:class:`data.news_sentiment.NewsSentimentFilter`: off by default, TTL-cached
(in :mod:`data.ratings`), and **fail-open** — a symbol with no ratings coverage,
a missing key, or any error passes through untouched (controlled by
``RATINGS_FAIL_OPEN``).

ETFs and any symbol the provider does not cover naturally return ``None`` from
the ratings lookup and are therefore approved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass
class RatingsResult:
    """Outcome of a per-symbol ratings check (mirrors ``SentimentResult``)."""

    symbol: str
    approved: bool
    rating: Optional[str]
    reason: str

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "approved": self.approved,
            "rating": self.rating,
            "reason": self.reason,
        }


class RatingsFilter:
    """Reject entries whose quant rating is below ``RATINGS_MIN``.

    Args:
        settings: Application settings (reads ``RATINGS_FILTER_ENABLED``,
            ``RATINGS_MIN``, ``RATINGS_FAIL_OPEN``).
        snapshot_fetcher: ``fetcher(symbol) -> RatingSnapshot | None``;
            defaults to :func:`data.ratings.get_rating_snapshot`.
    """

    def __init__(
        self,
        settings: Any,
        snapshot_fetcher: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self._settings = settings
        self._fetcher = snapshot_fetcher
        self._log = log.bind(component="RatingsFilter")

    def check(self, symbol: str) -> RatingsResult:
        """Return the ratings verdict for *symbol*.  Never raises."""
        from data.ratings import Rating

        symbol = str(symbol or "").upper()
        s = self._settings
        if not getattr(s, "RATINGS_FILTER_ENABLED", False):
            return RatingsResult(symbol, True, None, "ratings filter disabled")

        fail_open = bool(getattr(s, "RATINGS_FAIL_OPEN", True))
        minimum = Rating.from_str(getattr(s, "RATINGS_MIN", "hold")) or Rating.HOLD

        try:
            snap = self._resolve(symbol)
        except Exception as exc:  # noqa: BLE001 -- fail-open on any error
            self._log.warning("ratings_filter.lookup_failed", symbol=symbol, error=str(exc))
            return self._missing(symbol, fail_open, "ratings lookup failed")

        if snap is None or snap.quant_rating is None:
            return self._missing(symbol, fail_open, "no ratings coverage")

        rating = snap.quant_rating
        if rating >= minimum:
            return RatingsResult(
                symbol, True, rating.label,
                f"rating {rating.label} >= minimum {minimum.label}",
            )
        return RatingsResult(
            symbol, False, rating.label,
            f"rating {rating.label} below minimum {minimum.label}",
        )

    # ---------------------------------------------------------------- internal

    def _resolve(self, symbol: str) -> Any:
        if self._fetcher is not None:
            return self._fetcher(symbol)
        from data.ratings import get_rating_snapshot

        return get_rating_snapshot(symbol, self._settings)

    @staticmethod
    def _missing(symbol: str, fail_open: bool, why: str) -> "RatingsResult":
        if fail_open:
            return RatingsResult(symbol, True, None, f"{why} (fail-open)")
        return RatingsResult(symbol, False, None, f"{why} (fail-closed)")
