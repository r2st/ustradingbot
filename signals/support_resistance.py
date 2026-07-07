"""
Support/resistance level detection from daily price action (TA1).

Finds swing-high/low pivots (fractal detection with 2-bar wings), clusters
pivots that sit within ``0.5 x ATR(14)`` of each other into a single level,
and ranks levels by *touch count* (how many pivots landed in the cluster).
The dashboard's TA chart shades these as horizontal zones and the
explanation panel calls out the nearest resistance before a target.

Not to be confused with :mod:`execution.levels`, which manages exit ladders.

Usage::

    from signals.support_resistance import find_levels

    levels = find_levels(df)          # {"support": [...], "resistance": [...]}
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import structlog

log = structlog.get_logger(__name__)

#: Bars required on each side of a pivot for fractal detection.
PIVOT_WING = 2

#: Pivots within this multiple of ATR(14) are merged into one level.
CLUSTER_ATR_MULTIPLIER = 0.5

#: Levels returned per side (nearest to the reference price first).
MAX_LEVELS_PER_SIDE = 3

#: How many trailing bars to scan for pivots (matches the chart window
#: plus context so levels just off-screen still register).
PIVOT_LOOKBACK_BARS = 180


def find_pivots(
    df: pd.DataFrame,
    wing: int = PIVOT_WING,
) -> Tuple[List[float], List[float]]:
    """Return (pivot_high_prices, pivot_low_prices) via fractal detection.

    A pivot high is a bar whose ``High`` strictly exceeds the highs of the
    *wing* bars on both sides; pivot lows mirror that on ``Low``.

    Args:
        df: OHLCV DataFrame (``High``/``Low`` columns required).
        wing: Bars required on each side (default 2).

    Returns:
        Two lists of pivot prices (highs, lows), oldest first.
    """
    high = df["High"].astype(float).to_numpy()
    low = df["Low"].astype(float).to_numpy()
    n = len(df)
    highs: List[float] = []
    lows: List[float] = []
    for i in range(wing, n - wing):
        left_h = high[i - wing:i].max()
        right_h = high[i + 1:i + wing + 1].max()
        if high[i] > left_h and high[i] > right_h:
            highs.append(float(high[i]))
        left_l = low[i - wing:i].min()
        right_l = low[i + 1:i + wing + 1].min()
        if low[i] < left_l and low[i] < right_l:
            lows.append(float(low[i]))
    return highs, lows


def cluster_pivots(
    prices: List[float],
    tolerance: float,
) -> List[Dict[str, Any]]:
    """Cluster pivot prices within *tolerance* into levels with touch counts.

    Args:
        prices: Pivot prices (any order).
        tolerance: Max distance from the cluster mean to join it.

    Returns:
        ``[{"price": mean, "touches": count}, ...]`` sorted by price.
    """
    if not prices:
        return []
    clusters: List[List[float]] = []
    for p in sorted(prices):
        if clusters:
            current = clusters[-1]
            mean = sum(current) / len(current)
            if abs(p - mean) <= tolerance:
                current.append(p)
                continue
        clusters.append([p])
    return [
        {"price": round(sum(c) / len(c), 4), "touches": len(c)}
        for c in clusters
    ]


def _atr14(df: pd.DataFrame) -> Optional[float]:
    """ATR(14) — same math as ``combined_filter._compute_atr``."""
    if len(df) < 15:
        return None
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    value = float(true_range.rolling(window=14).mean().iloc[-1])
    return value if value == value else None  # NaN guard


def find_levels(
    df: pd.DataFrame,
    reference_price: Optional[float] = None,
    wing: int = PIVOT_WING,
    cluster_atr_multiplier: float = CLUSTER_ATR_MULTIPLIER,
    max_per_side: int = MAX_LEVELS_PER_SIDE,
    lookback: int = PIVOT_LOOKBACK_BARS,
) -> Dict[str, List[Dict[str, Any]]]:
    """Detect key support/resistance levels from price action.

    Args:
        df: OHLCV DataFrame with a DatetimeIndex.
        reference_price: Price that splits support (below) from resistance
            (above).  Defaults to the last close.
        wing: Fractal wing size for pivot detection.
        cluster_atr_multiplier: Cluster tolerance as a multiple of ATR(14).
        max_per_side: Levels returned per side, nearest first.
        lookback: Trailing bars scanned for pivots.

    Returns:
        ``{"support": [{"price", "touches"}, ...],
           "resistance": [{"price", "touches"}, ...]}`` — each side sorted
        nearest-to-reference first.  Empty lists on insufficient data.
    """
    empty: Dict[str, List[Dict[str, Any]]] = {"support": [], "resistance": []}
    if df is None or len(df) < (2 * wing + 1):
        return empty
    window = df.tail(lookback)
    try:
        ref = (
            float(reference_price)
            if reference_price
            else float(window["Close"].iloc[-1])
        )
    except (ValueError, TypeError, KeyError):
        return empty
    if ref <= 0:
        return empty

    atr = _atr14(window)
    # Fallback tolerance when ATR is unavailable: 0.5% of price.
    tolerance = (
        cluster_atr_multiplier * atr if atr and atr > 0 else ref * 0.005
    )

    highs, lows = find_pivots(window, wing=wing)
    levels = cluster_pivots(highs + lows, tolerance)

    support = [lv for lv in levels if lv["price"] < ref]
    resistance = [lv for lv in levels if lv["price"] > ref]
    support.sort(key=lambda lv: ref - lv["price"])
    resistance.sort(key=lambda lv: lv["price"] - ref)

    result = {
        "support": support[:max_per_side],
        "resistance": resistance[:max_per_side],
    }
    log.debug(
        "support_resistance.levels",
        reference=round(ref, 4),
        tolerance=round(tolerance, 4),
        support=result["support"],
        resistance=result["resistance"],
    )
    return result
