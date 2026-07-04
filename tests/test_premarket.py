"""Tests for the pre-market scanner (feature 14)."""

from __future__ import annotations

import pandas as pd

from config.settings import Settings
from signals.premarket import scan, scan_symbol


def _frame(closes, volumes):
    n = len(closes)
    idx = pd.bdate_range(end="2026-06-01", periods=n)
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": volumes},
        index=idx,
    )


def _settings(**kw):
    base = dict(PREMARKET_GAP_PCT=0.02, PREMARKET_VOLUME_RATIO=1.5)
    base.update(kw)
    return Settings(**base)


def test_gap_up_flagged():
    closes = [100.0] * 20 + [105.0]  # +5% gap
    hit = scan_symbol("AAPL", _frame(closes, [1_000_000] * 21), _settings())
    assert hit is not None and "gap_up" in hit.signals


def test_gap_down_flagged():
    closes = [100.0] * 20 + [95.0]  # -5% gap
    hit = scan_symbol("AAPL", _frame(closes, [1_000_000] * 21), _settings())
    assert hit is not None and "gap_down" in hit.signals


def test_high_volume_flagged():
    closes = [100.0] * 21  # no gap
    volumes = [1_000_000] * 20 + [3_000_000]  # 3x volume
    hit = scan_symbol("AAPL", _frame(closes, volumes), _settings())
    assert hit is not None and "high_volume" in hit.signals
    assert hit.volume_ratio >= 1.5


def test_quiet_frame_returns_none():
    closes = [100.0] * 21
    hit = scan_symbol("AAPL", _frame(closes, [1_000_000] * 21), _settings())
    assert hit is None


def test_too_few_rows_returns_none():
    assert scan_symbol("AAPL", _frame([100.0], [1_000_000]), _settings()) is None


def test_scan_sorts_and_skips_errors():
    frames = {
        "BIG": _frame([100.0] * 20 + [110.0], [1_000_000] * 21),  # +10%
        "SMALL": _frame([100.0] * 20 + [103.0], [1_000_000] * 21),  # +3%
    }

    def fetcher(sym):
        if sym == "BAD":
            raise RuntimeError("boom")
        return frames[sym]

    hits = scan(["SMALL", "BAD", "BIG"], _settings(), fetcher=fetcher)
    assert [h.symbol for h in hits] == ["BIG", "SMALL"]
