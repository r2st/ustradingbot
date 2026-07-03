from __future__ import annotations

"""RSI (Relative Strength Index) indicator calculation and scoring.

Computes RSI(14) using Wilder smoothing and scores bullish conviction
for momentum and swing strategies. Bearish divergence detection acts as
a hard disqualifier across all strategies.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd
import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RSIState:
    """Snapshot of RSI-derived signals for a single bar.

    Attributes:
        rsi_value: Current RSI(14) value.
        is_momentum_zone: RSI between 55 and 70 inclusive.
        is_swing_recovery: RSI dipped below 45 at some point in the last
            5 bars and is now rising (today > yesterday).
        is_overbought: RSI > 70.
        is_overbought_rollover: RSI was > 70 yesterday but is now <= 70.
        has_bearish_divergence: Price made a higher high while RSI made a
            lower high over the last 14 bars.
        is_above_midline: RSI > 50.
        is_rising: Today's RSI > yesterday's RSI.
        rsi_5_bars_ago: RSI value 5 bars ago.
    """

    rsi_value: float
    is_momentum_zone: bool
    is_swing_recovery: bool
    is_overbought: bool
    is_overbought_rollover: bool
    has_bearish_divergence: bool
    is_above_midline: bool
    is_rising: bool
    rsi_5_bars_ago: float


def calculate_rsi(df: pd.DataFrame, period: int = 14) -> RSIState:
    """Calculate RSI and derive all pattern flags from OHLCV data.

    Uses the standard Wilder smoothing method (exponential moving average
    of gains and losses) to compute RSI.

    Args:
        df: DataFrame with columns ``Open``, ``High``, ``Low``, ``Close``,
            ``Volume`` and a :class:`~pandas.DatetimeIndex`.  Must contain
            at least ``period + 1`` rows.
        period: Look-back window for RSI calculation.  Defaults to 14.

    Returns:
        An :class:`RSIState` capturing all computed signals for the most
        recent bar.

    Raises:
        ValueError: If the DataFrame has fewer rows than required.
    """
    required_rows = period + 1
    if len(df) < required_rows:
        raise ValueError(
            f"DataFrame must have at least {required_rows} rows, "
            f"got {len(df)}"
        )

    # --- RSI via Wilder smoothing -------------------------------------------
    close = df["Close"].astype(float)
    delta = close.diff()

    gains = delta.where(delta > 0, 0.0)
    losses = (-delta).where(delta < 0, 0.0)

    # First average is a simple mean over the initial *period* changes.
    avg_gain = gains.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = losses.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_series = 100.0 - (100.0 / (1.0 + rs))
    rsi_series = rsi_series.fillna(50.0)  # neutral when no losses

    current_rsi: float = float(rsi_series.iloc[-1])
    yesterday_rsi: float = float(rsi_series.iloc[-2])

    # --- Pattern detection --------------------------------------------------
    is_momentum_zone: bool = 55.0 <= current_rsi <= 70.0

    # Swing recovery: RSI dipped below 45 in last 5 bars AND is now rising
    lookback_5 = rsi_series.iloc[-6:-1]  # 5 bars before current
    dipped_below_45: bool = bool((lookback_5 < 45.0).any())
    is_rising: bool = current_rsi > yesterday_rsi
    is_swing_recovery: bool = dipped_below_45 and is_rising

    is_overbought: bool = current_rsi > 70.0
    is_overbought_rollover: bool = yesterday_rsi > 70.0 and current_rsi <= 70.0

    # Bearish divergence over last 14 bars
    has_bearish_divergence = _detect_bearish_divergence(
        close, rsi_series, lookback=14
    )

    is_above_midline: bool = current_rsi > 50.0

    # RSI 5 bars ago (or current if not enough history)
    rsi_5_bars_ago: float = (
        float(rsi_series.iloc[-6]) if len(rsi_series) >= 6 else current_rsi
    )

    state = RSIState(
        rsi_value=current_rsi,
        is_momentum_zone=is_momentum_zone,
        is_swing_recovery=is_swing_recovery,
        is_overbought=is_overbought,
        is_overbought_rollover=is_overbought_rollover,
        has_bearish_divergence=has_bearish_divergence,
        is_above_midline=is_above_midline,
        is_rising=is_rising,
        rsi_5_bars_ago=rsi_5_bars_ago,
    )

    log.debug(
        "rsi_calculated",
        rsi_value=round(current_rsi, 2),
        yesterday_rsi=round(yesterday_rsi, 2),
        rsi_5_bars_ago=round(rsi_5_bars_ago, 2),
        is_momentum_zone=is_momentum_zone,
        is_swing_recovery=is_swing_recovery,
        is_overbought=is_overbought,
        is_overbought_rollover=is_overbought_rollover,
        has_bearish_divergence=has_bearish_divergence,
        is_above_midline=is_above_midline,
        is_rising=is_rising,
    )

    return state


def _detect_bearish_divergence(
    close: pd.Series,
    rsi_series: pd.Series,
    lookback: int,
) -> bool:
    """Check for bearish divergence over the last *lookback* bars.

    Bearish divergence occurs when price makes a higher high while RSI
    makes a lower high — a sign that upward momentum is fading.

    Args:
        close: Close price series.
        rsi_series: Corresponding RSI series.
        lookback: Number of historical bars to inspect.

    Returns:
        ``True`` if bearish divergence is detected.
    """
    if len(close) < lookback + 1:
        return False

    # Historical window excludes the current bar
    hist_close = close.iloc[-(lookback + 1) : -1]
    hist_rsi = rsi_series.iloc[-(lookback + 1) : -1]

    highest_close_idx = hist_close.idxmax()
    highest_close_val: float = float(hist_close[highest_close_idx])
    rsi_at_highest_close: float = float(hist_rsi[highest_close_idx])

    current_close: float = float(close.iloc[-1])
    current_rsi: float = float(rsi_series.iloc[-1])

    divergence = current_close > highest_close_val and current_rsi < rsi_at_highest_close

    if divergence:
        log.debug(
            "bearish_divergence_detected",
            current_close=round(current_close, 2),
            highest_close=round(highest_close_val, 2),
            current_rsi=round(current_rsi, 2),
            rsi_at_highest_close=round(rsi_at_highest_close, 2),
        )

    return bool(divergence)


# Strategy name aliases mapping to canonical strategy keys.
_MOMENTUM_STRATEGIES = frozenset({"momentum", "vcp_breakout", "pead"})
_SWING_STRATEGIES = frozenset({"swing", "mean_reversion"})


def bullish_score(state: RSIState, strategy: str) -> float:
    """Score RSI conditions for bullish conviction.

    Returns a float in ``[0.0, 1.0]``.  A bearish divergence immediately
    yields ``0.0`` regardless of other factors.

    Args:
        state: Precomputed :class:`RSIState`.
        strategy: Strategy name (case-insensitive).  Recognised values:
            ``momentum``, ``vcp_breakout``, ``pead`` (momentum family);
            ``swing``, ``mean_reversion`` (swing family).

    Returns:
        Bullish score clamped to ``[0.0, 1.0]``.

    Raises:
        ValueError: If *strategy* is not recognised.
    """
    if state.has_bearish_divergence:
        log.debug(
            "rsi_bullish_score",
            strategy=strategy,
            score=0.0,
            reason="bearish_divergence_hard_zero",
        )
        return 0.0

    canonical = strategy.lower().strip()

    if canonical in _MOMENTUM_STRATEGIES:
        score, breakdown = _score_momentum(state)
    elif canonical in _SWING_STRATEGIES:
        score, breakdown = _score_swing(state)
    else:
        raise ValueError(
            f"Unknown strategy '{strategy}'. Expected one of: "
            f"{sorted(_MOMENTUM_STRATEGIES | _SWING_STRATEGIES)}"
        )

    clamped = max(0.0, min(1.0, score))

    log.debug(
        "rsi_bullish_score",
        strategy=canonical,
        raw_score=round(score, 4),
        clamped_score=round(clamped, 4),
        **breakdown,
    )

    return clamped


def _score_momentum(state: RSIState) -> tuple[float, dict[str, float]]:
    """Compute momentum-family bullish score.

    Components:
        - momentum_zone (RSI 55-70):       +0.40
        - rising_strongly (>5 pts over 5 bars): +0.20
        - above_midline (RSI > 50):        +0.20
        - not_overbought (RSI <= 70):      +0.20
        - overbought_penalty (RSI > 70):   -0.30
    """
    score = 0.0
    breakdown: dict[str, float] = {}

    if state.is_momentum_zone:
        score += 0.40
        breakdown["momentum_zone"] = 0.40
    else:
        breakdown["momentum_zone"] = 0.0

    rising_strongly = (state.rsi_value - state.rsi_5_bars_ago) >= 5.0
    if rising_strongly:
        score += 0.20
        breakdown["rising_strongly"] = 0.20
    else:
        breakdown["rising_strongly"] = 0.0

    if state.is_above_midline:
        score += 0.20
        breakdown["above_midline"] = 0.20
    else:
        breakdown["above_midline"] = 0.0

    if not state.is_overbought:
        score += 0.20
        breakdown["not_overbought"] = 0.20
    else:
        breakdown["not_overbought"] = 0.0

    if state.is_overbought:
        score -= 0.30
        breakdown["overbought_penalty"] = -0.30
    else:
        breakdown["overbought_penalty"] = 0.0

    return score, breakdown


def _score_swing(state: RSIState) -> tuple[float, dict[str, float]]:
    """Compute swing-family bullish score.

    Components:
        - swing_recovery:   +0.40
        - not_overbought:   +0.30
        - above_midline:    +0.15
        - rising:           +0.15
    """
    score = 0.0
    breakdown: dict[str, float] = {}

    if state.is_swing_recovery:
        score += 0.40
        breakdown["swing_recovery"] = 0.40
    else:
        breakdown["swing_recovery"] = 0.0

    if not state.is_overbought:
        score += 0.30
        breakdown["not_overbought"] = 0.30
    else:
        breakdown["not_overbought"] = 0.0

    if state.is_above_midline:
        score += 0.15
        breakdown["above_midline"] = 0.15
    else:
        breakdown["above_midline"] = 0.0

    if state.is_rising:
        score += 0.15
        breakdown["rising"] = 0.15
    else:
        breakdown["rising"] = 0.0

    return score, breakdown
