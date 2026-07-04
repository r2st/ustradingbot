"""
Multi-timeframe analysis: confirm daily signals against the weekly trend.

Daily signals are only worth taking when the higher timeframe agrees — a daily
long into a weekly downtrend is fighting the tide.  This module resamples the
daily OHLCV frame to weekly bars, classifies the weekly trend, and exposes a
single :func:`weekly_confirms` gate the screener consults before returning a
signal.

The weekly trend is *up* when the latest weekly close is above a rising
``WEEKLY_TREND_EMA_PERIOD``-week EMA, *down* when it is below a falling EMA, and
*neutral* otherwise.  Everything is derived from the same daily frame the
screener already fetched, so no extra data request is made.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from config.settings import Settings


@dataclass(frozen=True)
class WeeklyTrend:
    """Classified weekly trend for a symbol."""

    direction: str  # "up" | "down" | "neutral"
    weekly_close: float
    weekly_ema: float
    ema_rising: bool
    weeks: int

    @property
    def is_up(self) -> bool:
        return self.direction == "up"


def resample_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Resample a daily OHLCV frame to weekly (week ending Friday) bars."""
    if df is None or df.empty:
        return pd.DataFrame()
    if not isinstance(df.index, pd.DatetimeIndex):
        # Best effort: assume rows are ordered daily bars if no datetime index.
        df = df.copy()
        df.index = pd.date_range(end=pd.Timestamp.today(), periods=len(df), freq="B")
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    if "Volume" in df.columns:
        agg["Volume"] = "sum"
    weekly = df.resample("W-FRI").agg(agg).dropna(how="any")
    return weekly


def weekly_trend(df: pd.DataFrame, ema_period: int = 30) -> Optional[WeeklyTrend]:
    """Classify the weekly trend from a *daily* OHLCV frame.

    Returns ``None`` when there is not enough weekly history to compute the
    EMA (fewer than *ema_period* weekly bars).
    """
    weekly = resample_weekly(df)
    if weekly.empty or len(weekly) < ema_period:
        return None

    close = weekly["Close"].astype(float)
    ema = close.ewm(span=ema_period, adjust=False).mean()
    last_close = float(close.iloc[-1])
    last_ema = float(ema.iloc[-1])

    # "Rising" compares the EMA to its value ~4 weeks ago (a month of slope).
    lookback = min(4, len(ema) - 1)
    ema_rising = float(ema.iloc[-1]) > float(ema.iloc[-1 - lookback])

    if last_close > last_ema and ema_rising:
        direction = "up"
    elif last_close < last_ema and not ema_rising:
        direction = "down"
    else:
        direction = "neutral"

    return WeeklyTrend(
        direction=direction,
        weekly_close=round(last_close, 4),
        weekly_ema=round(last_ema, 4),
        ema_rising=ema_rising,
        weeks=int(len(weekly)),
    )


def weekly_confirms(
    df: pd.DataFrame,
    settings: Settings,
    direction: str = "long",
) -> bool:
    """Return whether a daily *direction* signal aligns with the weekly trend.

    Gated by settings:

    * ``ENABLE_MULTI_TIMEFRAME`` off → always confirms (feature disabled).
    * ``MTF_REQUIRE_WEEKLY_UPTREND`` off → confirms unless the weekly trend is
      explicitly *against* the trade (a softer filter).

    When the weekly trend cannot be computed (insufficient history) the signal
    is allowed through — multi-timeframe is a *filter*, not a hard requirement,
    and should never block a name purely for lacking a year of data.
    """
    if not settings.ENABLE_MULTI_TIMEFRAME:
        return True

    trend = weekly_trend(df, settings.WEEKLY_TREND_EMA_PERIOD)
    if trend is None:
        return True  # not enough weekly history -> don't block

    # Currently only long signals are produced.
    if direction == "long":
        if settings.MTF_REQUIRE_WEEKLY_UPTREND:
            return trend.direction == "up"
        return trend.direction != "down"
    # Short (future) — mirror the logic.
    if settings.MTF_REQUIRE_WEEKLY_UPTREND:
        return trend.direction == "down"
    return trend.direction != "up"
