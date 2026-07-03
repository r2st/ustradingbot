"""
Ripster EMA Cloud indicator calculation and scoring.

The Ripster EMA Clouds use two pairs of Exponential Moving Averages that
form visual "clouds" on a chart.  The gap between each pair is the cloud:

- **Fast cloud**: EMA(8) vs EMA(9)
- **Slow cloud**: EMA(34) vs EMA(35)

Bullish conviction is derived from the relative position of price and the
two clouds, whether the clouds are expanding, and whether a fresh
fast-above-slow crossover has occurred.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class RipsterState:
    """Snapshot of Ripster EMA Cloud conditions for a single bar.

    Attributes:
        ema8: 8-period EMA value.
        ema9: 9-period EMA value.
        ema34: 34-period EMA value.
        ema35: 35-period EMA value.
        fast_cloud_bullish: Fast cloud is green/bullish (``ema8 > ema9``).
        slow_cloud_bullish: Slow cloud is green/bullish (``ema34 > ema35``).
        price_above_fast: Price is above the fast cloud
            (``price > max(ema8, ema9)``).
        price_above_slow: Price is above the slow cloud
            (``price > max(ema34, ema35)``).
        fast_above_slow: Fast cloud is above the slow cloud
            (``ema8 > ema34``).
        clouds_expanding: The gap between the fast and slow clouds is
            growing compared to the previous bar.
        has_fresh_cross: Fast cloud just crossed above the slow cloud
            (yesterday ``ema8 <= ema34``, today ``ema8 > ema34``).
        price_below_both: Price is below both clouds
            (``price < min(ema9, ema35)``).
    """

    ema8: float
    ema9: float
    ema34: float
    ema35: float
    fast_cloud_bullish: bool
    slow_cloud_bullish: bool
    price_above_fast: bool
    price_above_slow: bool
    fast_above_slow: bool
    clouds_expanding: bool
    has_fresh_cross: bool
    price_below_both: bool


# ---------------------------------------------------------------------------
# EMA Cloud calculation
# ---------------------------------------------------------------------------


def calculate_ripster(df: pd.DataFrame) -> RipsterState:
    """Compute Ripster EMA Clouds and derive all pattern flags.

    Calculates four EMAs (8, 9, 34, 35) on the ``Close`` column using
    pandas ``ewm`` and detects cloud relationships, price position, and
    crossover events.

    Args:
        df: DataFrame with columns ``Open``, ``High``, ``Low``, ``Close``,
            ``Volume`` and a :class:`~pandas.DatetimeIndex`.  Must contain
            at least 36 rows (enough for the slowest EMA to stabilise).

    Returns:
        A :class:`RipsterState` capturing all computed signals for the
        most recent bar.

    Raises:
        ValueError: If the DataFrame has fewer rows than required.
    """
    min_rows = 36
    if len(df) < min_rows:
        raise ValueError(
            f"DataFrame must have at least {min_rows} rows, "
            f"got {len(df)}"
        )

    close = df["Close"].astype(float)
    price: float = float(close.iloc[-1])

    # --- Compute EMAs -------------------------------------------------------
    ema8_series = close.ewm(span=8, adjust=False).mean()
    ema9_series = close.ewm(span=9, adjust=False).mean()
    ema34_series = close.ewm(span=34, adjust=False).mean()
    ema35_series = close.ewm(span=35, adjust=False).mean()

    # Current bar values
    ema8: float = float(ema8_series.iloc[-1])
    ema9: float = float(ema9_series.iloc[-1])
    ema34: float = float(ema34_series.iloc[-1])
    ema35: float = float(ema35_series.iloc[-1])

    # Previous bar values (for crossover and expansion detection)
    prev_ema8: float = float(ema8_series.iloc[-2])
    prev_ema34: float = float(ema34_series.iloc[-2])

    # --- Cloud conditions ---------------------------------------------------
    fast_cloud_bullish: bool = ema8 > ema9
    slow_cloud_bullish: bool = ema34 > ema35

    # --- Price position relative to clouds ----------------------------------
    price_above_fast: bool = price > max(ema8, ema9)
    price_above_slow: bool = price > max(ema34, ema35)

    # --- Fast cloud vs slow cloud -------------------------------------------
    fast_above_slow: bool = ema8 > ema34

    # --- Cloud expansion detection ------------------------------------------
    # Gap = distance between the representative EMAs of each cloud.
    today_gap: float = abs(ema8 - ema34)
    yesterday_gap: float = abs(prev_ema8 - prev_ema34)
    clouds_expanding: bool = today_gap > yesterday_gap

    # --- Fresh crossover (bullish) ------------------------------------------
    has_fresh_cross: bool = (prev_ema8 <= prev_ema34) and (ema8 > ema34)

    # --- Price below both clouds --------------------------------------------
    price_below_both: bool = price < min(ema9, ema35)

    state = RipsterState(
        ema8=ema8,
        ema9=ema9,
        ema34=ema34,
        ema35=ema35,
        fast_cloud_bullish=fast_cloud_bullish,
        slow_cloud_bullish=slow_cloud_bullish,
        price_above_fast=price_above_fast,
        price_above_slow=price_above_slow,
        fast_above_slow=fast_above_slow,
        clouds_expanding=clouds_expanding,
        has_fresh_cross=has_fresh_cross,
        price_below_both=price_below_both,
    )

    log.debug(
        "ripster.calculated",
        price=round(price, 4),
        ema8=round(ema8, 4),
        ema9=round(ema9, 4),
        ema34=round(ema34, 4),
        ema35=round(ema35, 4),
        fast_cloud_bullish=fast_cloud_bullish,
        slow_cloud_bullish=slow_cloud_bullish,
        price_above_fast=price_above_fast,
        price_above_slow=price_above_slow,
        fast_above_slow=fast_above_slow,
        clouds_expanding=clouds_expanding,
        has_fresh_cross=has_fresh_cross,
        price_below_both=price_below_both,
    )

    return state


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def bullish_score(state: RipsterState) -> float:
    """Compute a bullish score in ``[0.0, 1.0]`` from Ripster cloud conditions.

    Scoring breakdown:
        * ``price_above_fast AND price_above_slow``: +0.30
          (if only ``price_above_slow``:             +0.15)
        * ``fast_cloud_bullish AND slow_cloud_bullish``: +0.25
          (if only ``fast_cloud_bullish``:                +0.10)
        * ``fast_above_slow``:                        +0.20
        * ``clouds_expanding``:                       +0.15
        * ``has_fresh_cross``:                        +0.10

    If ``price_below_both`` is ``True``, the function returns ``0.0``
    immediately (hard zero).

    Args:
        state: Precomputed :class:`RipsterState`.

    Returns:
        Bullish score clamped to ``[0.0, 1.0]``.
    """
    if state.price_below_both:
        log.debug(
            "ripster.bullish_score",
            score=0.0,
            reason="price_below_both_hard_zero",
        )
        return 0.0

    breakdown: dict[str, float] = {}
    score = 0.0

    # --- Price position (+0.30 or +0.15) ------------------------------------
    if state.price_above_fast and state.price_above_slow:
        breakdown["price_above_both"] = 0.30
        score += 0.30
    elif state.price_above_slow:
        breakdown["price_above_slow_only"] = 0.15
        score += 0.15
    else:
        breakdown["price_above_both"] = 0.0

    # --- Cloud colour (+0.25 or +0.10) --------------------------------------
    if state.fast_cloud_bullish and state.slow_cloud_bullish:
        breakdown["both_bullish"] = 0.25
        score += 0.25
    elif state.fast_cloud_bullish:
        breakdown["fast_bullish_only"] = 0.10
        score += 0.10
    else:
        breakdown["both_bullish"] = 0.0

    # --- Fast above slow (+0.20) --------------------------------------------
    if state.fast_above_slow:
        breakdown["fast_above_slow"] = 0.20
        score += 0.20
    else:
        breakdown["fast_above_slow"] = 0.0

    # --- Cloud expansion (+0.15) --------------------------------------------
    if state.clouds_expanding:
        breakdown["expanding"] = 0.15
        score += 0.15
    else:
        breakdown["expanding"] = 0.0

    # --- Fresh crossover (+0.10) --------------------------------------------
    if state.has_fresh_cross:
        breakdown["fresh_cross"] = 0.10
        score += 0.10
    else:
        breakdown["fresh_cross"] = 0.0

    clamped = float(np.clip(score, 0.0, 1.0))

    log.debug(
        "ripster.bullish_score",
        components=breakdown,
        raw=round(score, 4),
        clamped=round(clamped, 4),
    )

    return clamped
