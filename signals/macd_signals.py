"""
MACD (Moving Average Convergence Divergence) indicator calculation and scoring.

Computes MACD line, signal line, and histogram from price data, then
derives boolean conditions (crossovers, divergences, momentum) and a
bullish score in ``[0.0, 1.0]`` consumed by the scoring engine.
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
class MACDState:
    """Snapshot of MACD indicator values and derived conditions.

    Attributes:
        macd_line: MACD line value (fast EMA minus slow EMA).
        signal_line: Signal line value (EMA of MACD line).
        histogram: MACD histogram (``macd_line - signal_line``).
        has_bullish_crossover: MACD was below signal yesterday and is above
            signal today.
        is_confirmed_bullish: Bullish crossover occurred **and** MACD is
            above the zero line.
        has_histogram_flip: Histogram was negative yesterday and is positive
            today.
        is_momentum_intact: MACD > 0, histogram > 0, **and** histogram is
            growing (today's value exceeds yesterday's).
        has_bullish_divergence: Price made a lower low but histogram made a
            higher low over the last 20 bars.
    """

    macd_line: float
    signal_line: float
    histogram: float
    has_bullish_crossover: bool
    is_confirmed_bullish: bool
    has_histogram_flip: bool
    is_momentum_intact: bool
    has_bullish_divergence: bool


# ---------------------------------------------------------------------------
# MACD calculation
# ---------------------------------------------------------------------------


def _find_troughs(series: pd.Series) -> list[int]:
    """Return indices of local minima (troughs) in *series*.

    A trough is defined as a point where the previous value is greater (or
    equal) **and** the next value is greater (or equal), ensuring the
    candidate is a genuine local minimum.
    """
    troughs: list[int] = []
    values = series.values
    for i in range(1, len(values) - 1):
        if values[i] <= values[i - 1] and values[i] <= values[i + 1]:
            troughs.append(i)
    return troughs


def _detect_bullish_divergence(
    close: pd.Series,
    histogram: pd.Series,
    lookback: int = 20,
) -> bool:
    """Detect bullish divergence over the last *lookback* bars.

    Bullish divergence occurs when price makes a **lower low** while the
    histogram makes a **higher low** (less negative).  We compare the two
    most recent troughs in the histogram within the lookback window and
    the corresponding price lows.
    """
    if len(histogram) < lookback:
        log.debug(
            "macd.divergence_skip",
            reason="insufficient_bars",
            available=len(histogram),
            required=lookback,
        )
        return False

    hist_window = histogram.iloc[-lookback:]
    close_window = close.iloc[-lookback:]

    troughs = _find_troughs(hist_window)
    if len(troughs) < 2:
        log.debug(
            "macd.divergence_skip",
            reason="fewer_than_two_troughs",
            trough_count=len(troughs),
        )
        return False

    # Take the two most recent troughs.
    earlier_idx = troughs[-2]
    later_idx = troughs[-1]

    earlier_hist = hist_window.iloc[earlier_idx]
    later_hist = hist_window.iloc[later_idx]

    earlier_price = close_window.iloc[earlier_idx]
    later_price = close_window.iloc[later_idx]

    divergence = (later_price < earlier_price) and (later_hist > earlier_hist)

    log.debug(
        "macd.divergence_check",
        earlier_price=float(earlier_price),
        later_price=float(later_price),
        earlier_hist=float(earlier_hist),
        later_hist=float(later_hist),
        divergence=divergence,
    )
    return divergence


def calculate_macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> MACDState:
    """Compute the MACD indicator and derive boolean conditions.

    Parameters:
        df: OHLCV DataFrame with a ``DatetimeIndex`` and at least a
            ``Close`` column.
        fast: Period for the fast EMA (default ``12``).
        slow: Period for the slow EMA (default ``26``).
        signal: Period for the signal-line EMA (default ``9``).

    Returns:
        A :class:`MACDState` instance with the latest values and all
        derived conditions populated.

    Raises:
        ValueError: If the DataFrame has fewer rows than *slow + signal*
            (the minimum needed to produce a meaningful signal line).
    """
    min_rows = slow + signal
    if len(df) < min_rows:
        raise ValueError(
            f"DataFrame has {len(df)} rows but at least {min_rows} are "
            f"required (slow={slow}, signal={signal})."
        )

    close = df["Close"]

    # Standard EMA-based MACD.
    fast_ema = close.ewm(span=fast, adjust=False).mean()
    slow_ema = close.ewm(span=slow, adjust=False).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line

    # Current and previous bar values.
    curr_macd = float(macd_line.iloc[-1])
    curr_signal = float(signal_line.iloc[-1])
    curr_hist = float(histogram.iloc[-1])

    prev_macd = float(macd_line.iloc[-2])
    prev_signal = float(signal_line.iloc[-2])
    prev_hist = float(histogram.iloc[-2])

    # Derived conditions.
    has_bullish_crossover = (prev_macd < prev_signal) and (curr_macd > curr_signal)
    is_confirmed_bullish = has_bullish_crossover and (curr_macd > 0)
    has_histogram_flip = (prev_hist < 0) and (curr_hist > 0)
    is_momentum_intact = (
        curr_macd > 0 and curr_hist > 0 and curr_hist > prev_hist
    )
    has_bullish_divergence = _detect_bullish_divergence(close, histogram)

    log.debug(
        "macd.calculated",
        macd_line=curr_macd,
        signal_line=curr_signal,
        histogram=curr_hist,
        crossover=has_bullish_crossover,
        confirmed=is_confirmed_bullish,
        hist_flip=has_histogram_flip,
        momentum=is_momentum_intact,
        divergence=has_bullish_divergence,
    )

    return MACDState(
        macd_line=curr_macd,
        signal_line=curr_signal,
        histogram=curr_hist,
        has_bullish_crossover=has_bullish_crossover,
        is_confirmed_bullish=is_confirmed_bullish,
        has_histogram_flip=has_histogram_flip,
        is_momentum_intact=is_momentum_intact,
        has_bullish_divergence=has_bullish_divergence,
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

# Weight table for the bullish score components.
_WEIGHTS: dict[str, float] = {
    "crossover": 0.35,
    "confirmed": 0.20,
    "hist_flip": 0.15,
    "momentum_intact": 0.15,
    "divergence": 0.15,
}


def bullish_score(state: MACDState) -> float:
    """Compute a bullish score in ``[0.0, 1.0]`` from MACD conditions.

    Scoring breakdown:
        * ``has_bullish_crossover``:  +0.35
        * ``is_confirmed_bullish``:   +0.20
        * ``has_histogram_flip``:     +0.15
        * ``is_momentum_intact``:     +0.15
        * ``has_bullish_divergence``:  +0.15

    The result is clamped to ``[0.0, 1.0]``.
    """
    components: dict[str, float] = {
        "crossover": _WEIGHTS["crossover"] if state.has_bullish_crossover else 0.0,
        "confirmed": _WEIGHTS["confirmed"] if state.is_confirmed_bullish else 0.0,
        "hist_flip": _WEIGHTS["hist_flip"] if state.has_histogram_flip else 0.0,
        "momentum_intact": _WEIGHTS["momentum_intact"] if state.is_momentum_intact else 0.0,
        "divergence": _WEIGHTS["divergence"] if state.has_bullish_divergence else 0.0,
    }

    raw = sum(components.values())
    clamped = float(np.clip(raw, 0.0, 1.0))

    log.debug(
        "macd.bullish_score",
        components=components,
        raw=raw,
        clamped=clamped,
    )
    return clamped
