"""Tests for support/resistance detection (TA1, signals/support_resistance)."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from signals.support_resistance import (
    cluster_pivots,
    find_levels,
    find_pivots,
)


def _sawtooth_df(cycles: int = 8, half: int = 6) -> pd.DataFrame:
    """Price oscillating between ~90 (troughs) and ~110 (peaks).

    Each cycle produces one clean pivot high near 110 and one pivot low
    near 90, so the detector should find one strong resistance level and
    one strong support level.
    """
    closes = []
    for _ in range(cycles):
        # Drop each leg's endpoint so peaks/troughs appear exactly once
        # (a duplicated extreme would defeat strict fractal comparison).
        closes.extend(np.linspace(90, 110, half).tolist()[:-1])
        closes.extend(np.linspace(110, 90, half).tolist()[:-1])
    closes.append(100.0)  # reference close between the two levels
    n = len(closes)
    closes = np.array(closes)
    df = pd.DataFrame(
        {
            "Open": closes,
            "High": closes + 0.5,
            "Low": closes - 0.5,
            "Close": closes,
            "Volume": np.full(n, 1_000_000.0),
        },
        index=pd.bdate_range(end=datetime.now(), periods=n),
    )
    return df


# --------------------------------------------------------------------------- #
# pivots
# --------------------------------------------------------------------------- #


def test_find_pivots_detects_peaks_and_troughs() -> None:
    df = _sawtooth_df()
    highs, lows = find_pivots(df)
    assert len(highs) >= 6
    assert len(lows) >= 6
    assert all(h > 105 for h in highs)
    assert all(l < 95 for l in lows)


def test_find_pivots_flat_series_has_none() -> None:
    n = 30
    df = pd.DataFrame(
        {
            "Open": [100.0] * n, "High": [100.0] * n,
            "Low": [100.0] * n, "Close": [100.0] * n,
            "Volume": [1e6] * n,
        },
        index=pd.bdate_range(end=datetime.now(), periods=n),
    )
    highs, lows = find_pivots(df)
    assert highs == [] and lows == []


# --------------------------------------------------------------------------- #
# clustering
# --------------------------------------------------------------------------- #


def test_cluster_pivots_merges_within_tolerance() -> None:
    levels = cluster_pivots([100.0, 100.3, 99.8, 110.0, 110.4], tolerance=1.0)
    assert len(levels) == 2
    assert levels[0]["touches"] == 3
    assert levels[1]["touches"] == 2
    assert levels[0]["price"] == pytest.approx(100.03, abs=0.01)


def test_cluster_pivots_empty() -> None:
    assert cluster_pivots([], tolerance=1.0) == []


# --------------------------------------------------------------------------- #
# find_levels
# --------------------------------------------------------------------------- #


def test_find_levels_sawtooth_support_and_resistance() -> None:
    df = _sawtooth_df()
    levels = find_levels(df)
    assert levels["resistance"], "expected a resistance level near 110"
    assert levels["support"], "expected a support level near 90"
    top_res = levels["resistance"][0]
    top_sup = levels["support"][0]
    assert 105 < top_res["price"] < 115
    assert 85 < top_sup["price"] < 95
    # Repeated peaks/troughs must register as multiple touches.
    assert top_res["touches"] >= 3
    assert top_sup["touches"] >= 3


def test_find_levels_respects_max_per_side() -> None:
    df = _sawtooth_df()
    levels = find_levels(df, max_per_side=1)
    assert len(levels["support"]) <= 1
    assert len(levels["resistance"]) <= 1


def test_find_levels_reference_price_splits_sides() -> None:
    df = _sawtooth_df()
    # With a reference near the top, everything is support.
    levels = find_levels(df, reference_price=120.0)
    assert levels["resistance"] == []
    assert levels["support"]


def test_find_levels_insufficient_data() -> None:
    df = _sawtooth_df().head(3)
    assert find_levels(df) == {"support": [], "resistance": []}


def test_find_levels_none_df() -> None:
    assert find_levels(None) == {"support": [], "resistance": []}
