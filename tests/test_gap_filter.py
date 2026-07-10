"""Tests for extended-hours quotes (Feature 4) and the overnight-gap filter."""

from __future__ import annotations

from config.settings import Settings
from data.extended_hours import ExtQuote, _build_quote
from signals.gap_filter import GapEntryFilter
from signals.signal_types import Grade, Signal


def _sig(symbol="AAPL", direction="long") -> Signal:
    return Signal(
        symbol=symbol,
        strategy="momentum",
        direction=direction,
        entry_price=100.0,
        stop_price=97.0,
        target_price=106.0,
        signal_strength=0.8,
        grade=Grade.A,
    )


def _quote(gap_pct):
    return ExtQuote(
        symbol="AAPL", session="pre", last=100.0 * (1 + gap_pct),
        prev_close=100.0, gap_pct=gap_pct, ext_volume=None,
        avg_ext_volume=None, unusual=False,
    )


def _filter(**kw):
    base = dict(GAP_FILTER_ENABLED=True, GAP_DOWN_SKIP_PCT=-0.05,
                GAP_UP_CHASE_PCT=0.08, GAP_RESIZE_PCT=0.03, GAP_RESIZE_MODIFIER=0.5)
    base.update(kw)
    return Settings(**base)


# ── disabled / fail-open ────────────────────────────────────────────────────


def test_disabled_allows():
    f = GapEntryFilter(Settings(GAP_FILTER_ENABLED=False), quote_fetcher=lambda s: _quote(-0.10))
    r = f.check(_sig())
    assert r.allowed and r.action == "ok"


def test_no_quote_fails_open():
    f = GapEntryFilter(_filter(), quote_fetcher=lambda s: None)
    r = f.check(_sig())
    assert r.allowed and r.action == "ok"


def test_lookup_error_fails_open():
    def boom(_s):
        raise RuntimeError("down")

    f = GapEntryFilter(_filter(), quote_fetcher=boom)
    r = f.check(_sig())
    assert r.allowed and r.action == "ok"


# ── long-side thresholds ────────────────────────────────────────────────────


def test_long_gap_down_skips():
    f = GapEntryFilter(_filter(), quote_fetcher=lambda s: _quote(-0.06))
    r = f.check(_sig())
    assert not r.allowed and r.action == "skip"


def test_long_gap_up_chase_skips():
    f = GapEntryFilter(_filter(), quote_fetcher=lambda s: _quote(0.10))
    r = f.check(_sig())
    assert not r.allowed and r.action == "skip"


def test_long_moderate_adverse_resizes():
    f = GapEntryFilter(_filter(), quote_fetcher=lambda s: _quote(-0.04))
    r = f.check(_sig())
    assert r.allowed and r.action == "resize" and r.size_modifier == 0.5


def test_long_small_gap_ok():
    f = GapEntryFilter(_filter(), quote_fetcher=lambda s: _quote(0.01))
    r = f.check(_sig())
    assert r.allowed and r.action == "ok"


# ── short-side mirror ───────────────────────────────────────────────────────


def test_short_gap_up_skips():
    f = GapEntryFilter(_filter(), quote_fetcher=lambda s: _quote(0.06))
    r = f.check(_sig(direction="short"))
    assert not r.allowed and r.action == "skip"


# ── ExtQuote building ───────────────────────────────────────────────────────


def test_build_quote_computes_gap_and_unusual():
    settings = Settings(EXT_UNUSUAL_VOLUME_RATIO=3.0)
    raw = {"last": 110.0, "prev_close": 100.0, "session": "pre",
           "ext_volume": 400.0, "avg_ext_volume": 100.0}
    q = _build_quote("AAPL", raw, settings)
    assert q is not None
    assert abs(q.gap_pct - 0.10) < 1e-9
    assert q.unusual is True


def test_build_quote_none_on_missing_last():
    assert _build_quote("AAPL", {"prev_close": 100.0}, Settings()) is None
