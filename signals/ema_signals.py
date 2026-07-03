"""
EMA (Exponential Moving Average) structure analysis and scoring.

Computes four EMAs (9, 20, 50, 200) from OHLCV price data and derives
structural features used by the combined scoring engine: bullish stack,
partial stack, swing-entry zone, slope quality, and compression detection.

The :func:`bullish_score` function converts an :class:`EMAState` snapshot
into a normalised 0-1 score whose weighting depends on the active strategy
(momentum vs. swing).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import structlog

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SLOPE_LOOKBACK: int = 5
_SWING_PROXIMITY_PCT: float = 0.03
_COMPRESSION_PCT: float = 0.02

_SLOPE_WEIGHTS: dict[str, float] = {
    "ema9": 0.15,
    "ema20": 0.30,
    "ema50": 0.30,
    "ema200": 0.25,
}

_MOMENTUM_STRATEGIES: frozenset[str] = frozenset(
    {"momentum", "vcp_breakout", "pead"}
)
_SWING_STRATEGIES: frozenset[str] = frozenset({"swing", "mean_reversion"})


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EMAState:
    """Immutable snapshot of EMA structure for a single symbol.

    Attributes:
        ema9:  9-period exponential moving average.
        ema20: 20-period exponential moving average.
        ema50: 50-period exponential moving average.
        ema200: 200-period exponential moving average.
        price: Most recent closing price.
        is_above_ema200: ``True`` when *price* > *ema200* (bull regime).
        has_bullish_stack: Full stack — *price* > *ema9* > *ema20* > *ema50* > *ema200*.
        has_partial_stack: Relaxed ordering — *price* > *ema20* > *ema50*.
        is_swing_entry_zone: Price within 3 % of *ema20*, approached from above
            (*price* >= *ema20*), and *ema50* trending up.
        slope_quality: Weighted sum (0-1) of rising-EMA indicators over a
            5-bar lookback.
        is_compressed: *ema9*, *ema20*, *ema50* within 2 % spread of each other.
    """

    ema9: float
    ema20: float
    ema50: float
    ema200: float
    price: float
    is_above_ema200: bool
    has_bullish_stack: bool
    has_partial_stack: bool
    is_swing_entry_zone: bool
    slope_quality: float
    is_compressed: bool


# ---------------------------------------------------------------------------
# EMA calculation
# ---------------------------------------------------------------------------
def calculate_ema(df: pd.DataFrame) -> EMAState:
    """Compute EMA structure from an OHLCV DataFrame.

    Parameters:
        df: DataFrame with columns ``Open``, ``High``, ``Low``, ``Close``,
            ``Volume`` and a :class:`~pandas.DatetimeIndex`.  Must contain at
            least 200 rows for reliable EMA-200 values, though the function
            will work with fewer rows (the early EMA values will be less
            stable).

    Returns:
        An :class:`EMAState` snapshot derived from the latest bar.

    Raises:
        ValueError: If the DataFrame has fewer than ``_SLOPE_LOOKBACK + 1``
            rows (need at least 6 bars for slope comparison).
    """
    min_rows = _SLOPE_LOOKBACK + 1
    if len(df) < min_rows:
        raise ValueError(
            f"DataFrame must have at least {min_rows} rows, got {len(df)}"
        )

    close: pd.Series = df["Close"]

    # --- Compute EMAs -------------------------------------------------------
    ema9: pd.Series = close.ewm(span=9, adjust=False).mean()
    ema20: pd.Series = close.ewm(span=20, adjust=False).mean()
    ema50: pd.Series = close.ewm(span=50, adjust=False).mean()
    ema200: pd.Series = close.ewm(span=200, adjust=False).mean()

    # Latest values
    cur_ema9: float = float(ema9.iloc[-1])
    cur_ema20: float = float(ema20.iloc[-1])
    cur_ema50: float = float(ema50.iloc[-1])
    cur_ema200: float = float(ema200.iloc[-1])
    cur_price: float = float(close.iloc[-1])

    # Values from 5 bars ago
    prev_ema9: float = float(ema9.iloc[-min_rows])
    prev_ema20: float = float(ema20.iloc[-min_rows])
    prev_ema50: float = float(ema50.iloc[-min_rows])
    prev_ema200: float = float(ema200.iloc[-min_rows])

    # --- Regime & stack detection -------------------------------------------
    is_above_ema200: bool = cur_price > cur_ema200
    has_bullish_stack: bool = (
        cur_price > cur_ema9 > cur_ema20 > cur_ema50 > cur_ema200
    )
    has_partial_stack: bool = cur_price > cur_ema20 > cur_ema50

    # --- Swing entry zone ---------------------------------------------------
    ema50_uptrend: bool = cur_ema50 > prev_ema50
    within_proximity: bool = (
        abs(cur_price - cur_ema20) / cur_ema20 <= _SWING_PROXIMITY_PCT
    )
    approached_from_above: bool = cur_price >= cur_ema20
    is_swing_entry_zone: bool = (
        within_proximity and approached_from_above and ema50_uptrend
    )

    # --- Slope quality ------------------------------------------------------
    slope_quality: float = 0.0
    rising: dict[str, bool] = {
        "ema9": cur_ema9 > prev_ema9,
        "ema20": cur_ema20 > prev_ema20,
        "ema50": cur_ema50 > prev_ema50,
        "ema200": cur_ema200 > prev_ema200,
    }
    for name, weight in _SLOPE_WEIGHTS.items():
        if rising[name]:
            slope_quality += weight

    # --- Compression --------------------------------------------------------
    ema_vals: list[float] = [cur_ema9, cur_ema20, cur_ema50]
    min_val: float = min(ema_vals)
    max_val: float = max(ema_vals)
    is_compressed: bool = (max_val - min_val) / min_val < _COMPRESSION_PCT if min_val > 0 else False

    state = EMAState(
        ema9=cur_ema9,
        ema20=cur_ema20,
        ema50=cur_ema50,
        ema200=cur_ema200,
        price=cur_price,
        is_above_ema200=is_above_ema200,
        has_bullish_stack=has_bullish_stack,
        has_partial_stack=has_partial_stack,
        is_swing_entry_zone=is_swing_entry_zone,
        slope_quality=round(slope_quality, 4),
        is_compressed=is_compressed,
    )

    log.debug(
        "ema_state_computed",
        price=cur_price,
        ema9=cur_ema9,
        ema20=cur_ema20,
        ema50=cur_ema50,
        ema200=cur_ema200,
        bullish_stack=has_bullish_stack,
        partial_stack=has_partial_stack,
        swing_zone=is_swing_entry_zone,
        slope_quality=state.slope_quality,
        compressed=is_compressed,
        rising_emas=rising,
    )

    return state


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def bullish_score(state: EMAState, strategy: str) -> float:
    """Score EMA structure for a given trading strategy.

    Parameters:
        state: Pre-computed :class:`EMAState` for the symbol.
        strategy: One of ``"momentum"``, ``"vcp_breakout"``, ``"pead"``,
            ``"swing"``, or ``"mean_reversion"`` (case-insensitive).

    Returns:
        A score in ``[0.0, 1.0]`` reflecting how bullish the EMA structure
        looks for the requested strategy.  Returns ``0.0`` immediately if
        the price is below EMA-200 (bear regime).

    Raises:
        ValueError: If *strategy* is not recognised.
    """
    strategy_lower: str = strategy.lower()

    # --- Bear-regime hard zero ----------------------------------------------
    if not state.is_above_ema200:
        log.debug(
            "ema_bullish_score_bear_regime",
            strategy=strategy_lower,
            score=0.0,
        )
        return 0.0

    # --- Strategy dispatch --------------------------------------------------
    if strategy_lower in _MOMENTUM_STRATEGIES:
        score = _score_momentum(state)
    elif strategy_lower in _SWING_STRATEGIES:
        score = _score_swing(state)
    else:
        raise ValueError(
            f"Unknown strategy '{strategy}'. "
            f"Expected one of: {sorted(_MOMENTUM_STRATEGIES | _SWING_STRATEGIES)}"
        )

    # Clamp to [0.0, 1.0]
    score = float(np.clip(score, 0.0, 1.0))

    log.debug(
        "ema_bullish_score",
        strategy=strategy_lower,
        score=round(score, 4),
        above_ema200=state.is_above_ema200,
        bullish_stack=state.has_bullish_stack,
        partial_stack=state.has_partial_stack,
        swing_zone=state.is_swing_entry_zone,
        slope_quality=state.slope_quality,
    )

    return score


# ---------------------------------------------------------------------------
# Internal scoring helpers
# ---------------------------------------------------------------------------
def _score_momentum(state: EMAState) -> float:
    """Momentum / VCP-breakout / PEAD scoring.

    Breakdown:
        * bullish_stack:      +0.35  (full stack)
        * slope_quality:      +0.35 * slope (0-1)
        * healthy_ordering:   +0.20  (partial stack, no full stack)
        * full_stack_bonus:   +0.10  (full stack AND slope > 0.7)
    """
    breakdown: dict[str, float] = {}
    total: float = 0.0

    # Bullish stack vs. healthy ordering (mutually exclusive)
    if state.has_bullish_stack:
        breakdown["bullish_stack"] = 0.35
        total += 0.35
    elif state.has_partial_stack:
        breakdown["healthy_ordering"] = 0.20
        total += 0.20

    # Slope quality
    slope_contrib: float = 0.35 * state.slope_quality
    breakdown["slope_quality"] = round(slope_contrib, 4)
    total += slope_contrib

    # Full-stack bonus
    if state.has_bullish_stack and state.slope_quality > 0.7:
        breakdown["full_stack_bonus"] = 0.10
        total += 0.10

    log.debug("ema_momentum_breakdown", **breakdown)
    return total


def _score_swing(state: EMAState) -> float:
    """Swing / mean-reversion scoring.

    Breakdown:
        * swing_entry_zone: +0.40
        * slope_quality:    +0.35 * slope (0-1)
        * uptrend_intact:   +0.25  (partial OR full stack)
    """
    breakdown: dict[str, float] = {}
    total: float = 0.0

    # Swing entry zone
    if state.is_swing_entry_zone:
        breakdown["swing_entry_zone"] = 0.40
        total += 0.40

    # Slope quality
    slope_contrib: float = 0.35 * state.slope_quality
    breakdown["slope_quality"] = round(slope_contrib, 4)
    total += slope_contrib

    # Uptrend intact
    if state.has_partial_stack or state.has_bullish_stack:
        breakdown["uptrend_intact"] = 0.25
        total += 0.25

    log.debug("ema_swing_breakdown", **breakdown)
    return total
