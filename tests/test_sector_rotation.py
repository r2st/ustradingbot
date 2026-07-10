"""Tests for the sector-rotation strategy and market-breadth indicator (F3)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.breadth import sector_breadth
from config.settings import Settings
from signals.sector_rotation import rank_sectors, run_sector_rotation_scan


def _frame(drift: float, n: int = 160, start: float = 100.0) -> pd.DataFrame:
    """A deterministic OHLCV frame with a constant per-bar drift."""
    idx = pd.bdate_range(end="2026-07-10", periods=n)
    closes = start * np.cumprod(np.full(n, 1.0 + drift))
    high = closes * 1.005
    low = closes * 0.995
    return pd.DataFrame(
        {"Open": closes, "High": high, "Low": low, "Close": closes,
         "Volume": np.full(n, 1_000_000.0)},
        index=idx,
    )


# Leaders rise faster than SPY; laggards fall.
_STRONG = _frame(0.004)   # ~ strong uptrend
_WEAK = _frame(-0.003)    # downtrend
_SPY = _frame(0.001)      # modest market


def _fetcher(mapping):
    def fetch(symbol):
        return mapping.get(symbol.upper(), _frame(0.001))
    return fetch


def _settings(**kw) -> Settings:
    base = dict(SECTOR_ROTATION_ENABLED=True, SECTOR_ROTATION_TOP_N=3,
                SECTOR_ROTATION_LOOKBACK_DAYS=63)
    base.update(kw)
    return Settings(**base)


# ── ranking ─────────────────────────────────────────────────────────────────


def test_rank_sectors_orders_by_relative_strength():
    mapping = {"SPY": _SPY, "XLK": _STRONG, "XLU": _WEAK}
    ranks = rank_sectors(_settings(), fetcher=_fetcher(mapping))
    assert ranks, "expected some ranked sectors"
    etfs = [r.etf for r in ranks]
    # XLK (strong) should rank ahead of XLU (weak).
    assert etfs.index("XLK") < etfs.index("XLU")
    xlk = next(r for r in ranks if r.etf == "XLK")
    assert xlk.rel_strength > 0 and xlk.above_ma50


def test_rank_handles_benchmark_failure():
    # Fetcher raises for SPY -> bench_ret degrades to 0, still ranks sectors.
    def fetch(symbol):
        if symbol.upper() == "SPY":
            raise RuntimeError("no spy")
        return _STRONG if symbol.upper() == "XLK" else _WEAK

    ranks = rank_sectors(_settings(), fetcher=fetch)
    assert any(r.etf == "XLK" for r in ranks)


# ── scan ────────────────────────────────────────────────────────────────────


def test_scan_emits_leader_signals():
    mapping = {"SPY": _SPY}
    # Make three sector ETFs strong leaders.
    for etf in ("XLK", "XLF", "XLE"):
        mapping[etf] = _STRONG
    sigs = run_sector_rotation_scan(_settings(SECTOR_ROTATION_TOP_N=3),
                                    fetcher=_fetcher(mapping), min_grade="C")
    assert 1 <= len(sigs) <= 3
    for s in sigs:
        assert s.strategy == "sector_rotation"
        assert s.direction == "long"
        assert s.entry_price > s.stop_price
        assert s.target_price > s.entry_price
        assert "sector" in s.raw_data


def test_scan_respects_top_n():
    mapping = {"SPY": _SPY}
    for etf in ("XLK", "XLF", "XLE", "XLV", "XLI"):
        mapping[etf] = _STRONG
    sigs = run_sector_rotation_scan(_settings(SECTOR_ROTATION_TOP_N=2),
                                    fetcher=_fetcher(mapping), min_grade="C")
    assert len(sigs) <= 2


def test_scan_skips_non_leaders():
    # Every sector weaker than SPY -> no leaders -> no signals.
    def fetch(symbol):
        return _frame(0.003) if symbol.upper() == "SPY" else _WEAK

    sigs = run_sector_rotation_scan(_settings(), fetcher=fetch, min_grade="C")
    assert sigs == []


# ── breadth ─────────────────────────────────────────────────────────────────


def test_breadth_counts_participation():
    # All sector ETFs strongly above their 50-day MA.
    result = sector_breadth(fetcher=lambda s: _STRONG)
    assert result.total == 11 and result.above == 11
    assert result.pct == 1.0 and result.label == "strong"


def test_breadth_weak_tape():
    result = sector_breadth(fetcher=lambda s: _WEAK)
    assert result.above == 0 and result.label == "weak"


def test_breadth_unknown_on_no_data():
    result = sector_breadth(fetcher=lambda s: None)
    assert result.total == 0 and result.label == "unknown"
