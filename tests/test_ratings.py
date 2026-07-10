"""Tests for the ratings provider (Feature 5) and the ratings entry filter."""

from __future__ import annotations

from config.settings import Settings
from data.ratings import (
    FinnhubRatingsProvider,
    Rating,
    RatingSnapshot,
    clear_cache,
    get_rating_snapshot,
    rating_from_score,
)
from signals.ratings_filter import RatingsFilter


def setup_function(_fn):
    clear_cache()


# ── Rating enum / scoring ───────────────────────────────────────────────────


def test_rating_ordering():
    assert Rating.STRONG_BUY > Rating.BUY > Rating.HOLD > Rating.SELL > Rating.STRONG_SELL


def test_rating_from_str():
    assert Rating.from_str("hold") == Rating.HOLD
    assert Rating.from_str("Strong Buy") == Rating.STRONG_BUY
    assert Rating.from_str("nonsense") is None


def test_rating_from_score_bands():
    assert rating_from_score(5.0) == Rating.STRONG_BUY
    assert rating_from_score(3.0) == Rating.HOLD
    assert rating_from_score(1.0) == Rating.STRONG_SELL


# ── Finnhub provider (injected http_get) ────────────────────────────────────


def _finnhub_http(recs, target=None):
    def http_get(url, params):
        if "recommendation" in url:
            return recs
        if "price-target" in url:
            return target or {}
        return {}

    return http_get


def test_finnhub_provider_builds_snapshot():
    recs = [
        {"period": "2026-05-01", "strongBuy": 10, "buy": 5, "hold": 1, "sell": 0, "strongSell": 0},
        {"period": "2026-06-01", "strongBuy": 12, "buy": 4, "hold": 1, "sell": 0, "strongSell": 0},
    ]
    provider = FinnhubRatingsProvider(
        Settings(FINNHUB_API_KEY="x"),
        http_get=_finnhub_http(recs, {"targetMean": 250.0}),
    )
    snap = provider.get_rating("AAPL")
    assert isinstance(snap, RatingSnapshot)
    assert snap.quant_rating in (Rating.BUY, Rating.STRONG_BUY)
    assert snap.price_target == 250.0
    assert snap.as_of == "2026-06-01"


def test_finnhub_upside_pct():
    snap = RatingSnapshot("AAPL", Rating.BUY, price_target=120.0)
    assert snap.upside_pct(100.0) == 20.0
    assert snap.upside_pct(None) is None


def test_finnhub_detects_downgrade():
    recs = [
        {"period": "2026-05-01", "strongBuy": 12, "buy": 4, "hold": 0, "sell": 0, "strongSell": 0},
        {"period": "2026-06-01", "strongBuy": 0, "buy": 1, "hold": 10, "sell": 3, "strongSell": 0},
    ]
    provider = FinnhubRatingsProvider(Settings(FINNHUB_API_KEY="x"), http_get=_finnhub_http(recs))
    snap = provider.get_rating("AAPL")
    assert snap is not None and snap.recent_change is not None
    assert "downgrade" in snap.recent_change


# ── RatingsFilter ───────────────────────────────────────────────────────────


def _snap(rating):
    return RatingSnapshot("AAPL", rating)


def test_filter_disabled_allows():
    f = RatingsFilter(Settings(RATINGS_FILTER_ENABLED=False), snapshot_fetcher=lambda s: _snap(Rating.STRONG_SELL))
    assert f.check("AAPL").approved


def test_filter_rejects_below_minimum():
    settings = Settings(RATINGS_FILTER_ENABLED=True, RATINGS_MIN="hold")
    f = RatingsFilter(settings, snapshot_fetcher=lambda s: _snap(Rating.SELL))
    r = f.check("AAPL")
    assert not r.approved and "below minimum" in r.reason


def test_filter_allows_at_or_above_minimum():
    settings = Settings(RATINGS_FILTER_ENABLED=True, RATINGS_MIN="hold")
    f = RatingsFilter(settings, snapshot_fetcher=lambda s: _snap(Rating.BUY))
    assert f.check("AAPL").approved


def test_filter_missing_coverage_fail_open():
    settings = Settings(RATINGS_FILTER_ENABLED=True, RATINGS_FAIL_OPEN=True)
    f = RatingsFilter(settings, snapshot_fetcher=lambda s: None)
    assert f.check("XLK").approved


def test_filter_missing_coverage_fail_closed():
    settings = Settings(RATINGS_FILTER_ENABLED=True, RATINGS_FAIL_OPEN=False)
    f = RatingsFilter(settings, snapshot_fetcher=lambda s: None)
    assert not f.check("AAPL").approved


def test_filter_lookup_error_fail_open():
    settings = Settings(RATINGS_FILTER_ENABLED=True, RATINGS_FAIL_OPEN=True)

    def boom(_s):
        raise RuntimeError("down")

    f = RatingsFilter(settings, snapshot_fetcher=boom)
    assert f.check("AAPL").approved


# ── cached accessor ─────────────────────────────────────────────────────────


def test_build_ratings_card_block():
    from dashboard.analyst_cards import build_ratings

    row = {
        "price": 100.0,
        "ratings": RatingSnapshot("AAPL", Rating.BUY, price_target=120.0,
                                  consensus="Buy", as_of="2026-06-01").to_dict(),
    }
    block = build_ratings(row)
    assert block is not None
    assert block["quant_rating"] == "Buy"
    assert block["upside_pct"] == 20.0


def test_build_ratings_none_when_absent():
    from dashboard.analyst_cards import build_ratings

    assert build_ratings({"price": 100.0}) is None


def test_get_rating_snapshot_caches():
    calls = {"n": 0}

    class P:
        name = "stub"

        def get_rating(self, symbol):
            calls["n"] += 1
            return _snap(Rating.BUY)

    settings = Settings(RATINGS_FILTER_ENABLED=True)
    get_rating_snapshot("AAPL", settings, provider=P())
    get_rating_snapshot("AAPL", settings, provider=P())
    assert calls["n"] == 1
