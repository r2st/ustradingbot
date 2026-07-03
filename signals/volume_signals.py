"""
Volume analysis and scoring for the trading bot.

Computes On-Balance Volume (OBV), volume ratios, VWAP, and bullish/bearish
surge detection.  Exposes :func:`calculate_volume` to derive a
:class:`VolumeState` snapshot from an OHLCV DataFrame and
:func:`bullish_score` to convert that state into a 0.0-1.0 score whose
weighting depends on the active strategy.
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

_AVG_VOLUME_WINDOW: int = 20
_OBV_LOOKBACK: int = 10
_VWAP_WINDOW: int = 20
_SURGE_MULTIPLIER: float = 1.5
_DRYUP_MULTIPLIER: float = 0.5


# ---------------------------------------------------------------------------
# VolumeState dataclass
# ---------------------------------------------------------------------------


@dataclass
class VolumeState:
    """Snapshot of volume-derived indicators for a single symbol.

    Attributes:
        volume_ratio: Today's volume divided by the 20-day average volume.
        has_bullish_surge: Up day (close > open) with volume >= 1.5x the
            20-day average.
        has_bullish_dryup: Down day (close < open) with volume <= 0.5x the
            20-day average.
        is_obv_confirming: OBV today exceeds OBV 10 bars ago, confirming
            the prevailing trend.
        has_obv_divergence: Price rose over the last 10 bars while OBV
            fell -- a bearish divergence warning.
        is_above_vwap: Current price is above the 20-bar rolling VWAP.
        has_bearish_surge: Down day (close < open) with volume > 1.5x the
            20-day average.
    """

    volume_ratio: float
    has_bullish_surge: bool
    has_bullish_dryup: bool
    is_obv_confirming: bool
    has_obv_divergence: bool
    is_above_vwap: bool
    has_bearish_surge: bool


# ---------------------------------------------------------------------------
# Core calculation
# ---------------------------------------------------------------------------


def calculate_volume(df: pd.DataFrame) -> VolumeState:
    """Derive a :class:`VolumeState` from an OHLCV DataFrame.

    Parameters:
        df: DataFrame with columns ``Open``, ``High``, ``Low``, ``Close``,
            ``Volume`` and a :class:`~pandas.DatetimeIndex`.

    Returns:
        A fully-populated :class:`VolumeState` for the most recent bar.

    Raises:
        ValueError: If the DataFrame has fewer rows than required for the
            lookback windows.
    """
    min_rows = max(_AVG_VOLUME_WINDOW, _OBV_LOOKBACK, _VWAP_WINDOW) + 1
    if len(df) < min_rows:
        raise ValueError(
            f"DataFrame has {len(df)} rows; at least {min_rows} are required"
        )

    close: pd.Series = df["Close"]
    open_: pd.Series = df["Open"]
    volume: pd.Series = df["Volume"]

    # -- OBV ----------------------------------------------------------------
    price_diff = close.diff()
    obv_direction = np.sign(price_diff).fillna(0).astype(int)
    obv: pd.Series = (obv_direction * volume).cumsum()

    # -- 20-day average volume (SMA) ----------------------------------------
    avg_volume: float = float(volume.iloc[-_AVG_VOLUME_WINDOW:].mean())

    # -- Volume ratio -------------------------------------------------------
    current_volume: float = float(volume.iloc[-1])
    volume_ratio: float = current_volume / avg_volume if avg_volume > 0 else 0.0

    # -- Up / down day flags ------------------------------------------------
    is_up_day: bool = float(close.iloc[-1]) > float(open_.iloc[-1])
    is_down_day: bool = float(close.iloc[-1]) < float(open_.iloc[-1])

    # -- Bullish / bearish surge & dryup ------------------------------------
    has_bullish_surge: bool = is_up_day and volume_ratio >= _SURGE_MULTIPLIER
    has_bullish_dryup: bool = is_down_day and volume_ratio <= _DRYUP_MULTIPLIER
    has_bearish_surge: bool = is_down_day and volume_ratio > _SURGE_MULTIPLIER

    # -- OBV confirmation & divergence --------------------------------------
    obv_current: float = float(obv.iloc[-1])
    obv_past: float = float(obv.iloc[-1 - _OBV_LOOKBACK])
    is_obv_confirming: bool = obv_current > obv_past

    close_current: float = float(close.iloc[-1])
    close_past: float = float(close.iloc[-1 - _OBV_LOOKBACK])
    price_went_up: bool = close_current > close_past
    obv_went_down: bool = obv_current < obv_past
    has_obv_divergence: bool = price_went_up and obv_went_down

    # -- Rolling 20-day VWAP -----------------------------------------------
    recent_close = close.iloc[-_VWAP_WINDOW:]
    recent_volume = volume.iloc[-_VWAP_WINDOW:]
    sum_cv: float = float((recent_close * recent_volume).sum())
    sum_v: float = float(recent_volume.sum())
    rolling_vwap: float = sum_cv / sum_v if sum_v > 0 else 0.0
    is_above_vwap: bool = close_current > rolling_vwap

    state = VolumeState(
        volume_ratio=volume_ratio,
        has_bullish_surge=has_bullish_surge,
        has_bullish_dryup=has_bullish_dryup,
        is_obv_confirming=is_obv_confirming,
        has_obv_divergence=has_obv_divergence,
        is_above_vwap=is_above_vwap,
        has_bearish_surge=has_bearish_surge,
    )

    log.debug(
        "volume_state_calculated",
        volume_ratio=round(volume_ratio, 4),
        avg_volume=round(avg_volume, 2),
        obv_current=round(obv_current, 2),
        obv_past=round(obv_past, 2),
        rolling_vwap=round(rolling_vwap, 4),
        is_up_day=is_up_day,
        state=state,
    )

    return state


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

# Strategy aliases (lowercased)
_MOMENTUM_STRATEGIES: frozenset[str] = frozenset(
    {"momentum", "vcp_breakout", "pead"}
)
_SWING_STRATEGIES: frozenset[str] = frozenset(
    {"swing", "mean_reversion"}
)


def bullish_score(state: VolumeState, strategy: str) -> float:
    """Score the volume state on a 0.0-1.0 scale for the given strategy.

    Parameters:
        state: A :class:`VolumeState` produced by :func:`calculate_volume`.
        strategy: Active strategy name (case-insensitive).  Recognised
            strategies: ``momentum``, ``vcp_breakout``, ``pead`` (momentum
            weighting); ``swing``, ``mean_reversion`` (swing weighting).

    Returns:
        A float clamped to ``[0.0, 1.0]``.
    """
    strategy_lower = strategy.lower()

    # Hard zero on OBV divergence
    if state.has_obv_divergence:
        log.debug(
            "volume_bullish_score_hard_zero",
            reason="obv_divergence",
            strategy=strategy_lower,
        )
        return 0.0

    breakdown: dict[str, float] = {}

    if strategy_lower in _MOMENTUM_STRATEGIES:
        breakdown["surge"] = 0.40 if state.has_bullish_surge else 0.0
        breakdown["obv_confirming"] = 0.25 if state.is_obv_confirming else 0.0
        breakdown["above_vwap"] = 0.20 if state.is_above_vwap else 0.0
        breakdown["no_bearish"] = 0.15 if not state.has_bearish_surge else 0.0
    elif strategy_lower in _SWING_STRATEGIES:
        breakdown["dryup"] = 0.40 if state.has_bullish_dryup else 0.0
        breakdown["obv_confirming"] = 0.25 if state.is_obv_confirming else 0.0
        breakdown["near_vwap"] = 0.20 if state.is_above_vwap else 0.0
        breakdown["no_bearish"] = 0.15 if not state.has_bearish_surge else 0.0
    else:
        # Unknown strategy -- fall back to momentum weighting
        log.debug(
            "volume_bullish_score_unknown_strategy",
            strategy=strategy_lower,
            fallback="momentum",
        )
        breakdown["surge"] = 0.40 if state.has_bullish_surge else 0.0
        breakdown["obv_confirming"] = 0.25 if state.is_obv_confirming else 0.0
        breakdown["above_vwap"] = 0.20 if state.is_above_vwap else 0.0
        breakdown["no_bearish"] = 0.15 if not state.has_bearish_surge else 0.0

    raw_score: float = sum(breakdown.values())
    clamped: float = max(0.0, min(1.0, raw_score))

    log.debug(
        "volume_bullish_score",
        strategy=strategy_lower,
        breakdown=breakdown,
        raw_score=round(raw_score, 4),
        clamped=round(clamped, 4),
    )

    return clamped
