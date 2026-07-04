"""Tests for the news sentiment filter (feature 4)."""

from __future__ import annotations

from config.settings import Settings
from data.news_sentiment import NewsSentimentFilter, score_headlines


def _settings(**kw):
    base = dict(NEWS_SENTIMENT_ENABLED=True, FINNHUB_API_KEY="x", NEWS_MIN_ARTICLES=2)
    base.update(kw)
    return Settings(**base)


def _articles(headlines):
    return [{"headline": h, "summary": "", "datetime": 0} for h in headlines]


def test_score_positive_negative_neutral():
    assert score_headlines(["record profit and strong growth, upgrade"]) > 0
    assert score_headlines(["fraud probe and lawsuit, bankruptcy warning"]) < 0
    assert score_headlines(["the company held a meeting today"]) == 0.0
    assert score_headlines([]) == 0.0


def test_disabled_filter_approves():
    f = NewsSentimentFilter(Settings(NEWS_SENTIMENT_ENABLED=False), fetcher=lambda s: [])
    r = f.check("AAPL")
    assert r.approved and "disabled" in r.reason


def test_no_key_with_default_fetcher_approves():
    f = NewsSentimentFilter(Settings(NEWS_SENTIMENT_ENABLED=True, FINNHUB_API_KEY=""))
    assert f.check("AAPL").approved


def test_strong_negative_rejects():
    news = _articles(["Massive fraud probe and lawsuit", "Shares plunge on bankruptcy warning"])
    f = NewsSentimentFilter(_settings(), fetcher=lambda s: news)
    r = f.check("AAPL")
    assert not r.approved
    assert r.score < 0
    assert r.article_count == 2


def test_positive_approves():
    news = _articles(["Record profit beats estimates", "Analyst upgrade, shares surge"])
    f = NewsSentimentFilter(_settings(), fetcher=lambda s: news)
    r = f.check("AAPL")
    assert r.approved and r.score > 0


def test_insufficient_articles_fail_open():
    f = NewsSentimentFilter(_settings(NEWS_MIN_ARTICLES=3), fetcher=lambda s: _articles(["plunge fraud"]))
    r = f.check("AAPL")
    assert r.approved and "insufficient" in r.reason


def test_fetcher_error_fail_open():
    def boom(_s):
        raise RuntimeError("network down")

    f = NewsSentimentFilter(_settings(), fetcher=boom)
    r = f.check("AAPL")
    assert r.approved and "fail-open" in r.reason


def test_provided_sentiment_used():
    news = [
        {"headline": "neutral wording", "summary": "", "datetime": 0, "sentiment": -0.9},
        {"headline": "neutral wording", "summary": "", "datetime": 0, "sentiment": -0.8},
    ]
    f = NewsSentimentFilter(_settings(NEWS_SENTIMENT_MIN_SCORE=-0.5), fetcher=lambda s: news)
    r = f.check("AAPL")
    assert not r.approved  # provided sentiment drives the decision, not the lexicon


def test_cache_hits_fetcher_once():
    calls = {"n": 0}

    def counting(_s):
        calls["n"] += 1
        return _articles(["record profit beats", "upgrade surge"])

    clock = {"t": 0.0}
    f = NewsSentimentFilter(_settings(NEWS_CACHE_TTL_MINUTES=30.0), fetcher=counting,
                            clock=lambda: clock["t"])
    f.check("AAPL")
    f.check("AAPL")
    assert calls["n"] == 1
    # Advance beyond TTL -> refetch.
    clock["t"] = 30 * 60 + 1
    f.check("AAPL")
    assert calls["n"] == 2
