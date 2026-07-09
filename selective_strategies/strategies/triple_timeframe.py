"""
Strategy B -- Triple-Timeframe Trend Confluence Breakout.

Since we only have daily data, lower timeframes are simulated:
  - Daily = macro trend (SMA50 and SMA200)
  - 4H proxy = recent consolidation detection (contracting ATR, narrow range)
  - 1H proxy = breakout bar check on the latest daily bar

Enters when all three "timeframes" align: macro trend confirmed by SMA stack,
consolidation detected via ATR contraction, and a breakout bar closes beyond
the consolidation range on elevated volume.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import structlog

from selective_strategies.config import TripleTimeframeConfig
from selective_strategies.events import has_upcoming_event
from selective_strategies.signal import SelectiveSignal
from short_strategies.common.indicators import atr, rsi, sma, ema, volume_ratio

log = structlog.get_logger(__name__)

STRATEGY_ID = "hs_triple_timeframe"


def detect(
    symbol: str,
    df: pd.DataFrame,
    config=None,
    filters=None,
    ctx=None,
) -> Optional[SelectiveSignal]:
    """Detect triple-timeframe breakout on the last bar.  Never raises."""
    try:
        cfg = config or TripleTimeframeConfig()
        if df is None or len(df) < 200:
            return None

        close = df["Close"].astype(float)
        price = float(close.iloc[-1])
        if price <= 0:
            return None

        sma_fast = sma(close, cfg.sma_fast)
        sma_slow = sma(close, cfg.sma_slow)
        atr_value = atr(df, 14)
        vol_ratio = volume_ratio(df, 20)

        if atr_value is None or vol_ratio is None:
            return None

        sma_fast_val = float(sma_fast.iloc[-1])
        sma_slow_val = float(sma_slow.iloc[-1])
        if pd.isna(sma_fast_val) or pd.isna(sma_slow_val):
            return None

        # SMA50 slope over last N days
        sma_fast_prev = float(sma_fast.iloc[-cfg.slope_lookback])
        if pd.isna(sma_fast_prev):
            return None
        sma_slope = sma_fast_val - sma_fast_prev

        # Determine direction
        direction = None
        if price > sma_fast_val and price > sma_slow_val and sma_slope > 0:
            direction = "long"
        elif price < sma_fast_val and price < sma_slow_val and sma_slope < 0:
            direction = "short"
        else:
            return None

        # Consolidation detection: look at last N bars
        consol_bars = cfg.consolidation_bars
        if len(df) < consol_bars + 20:
            return None
        consol_slice = df.iloc[-(consol_bars + 1):-1]  # exclude today
        range_high = float(consol_slice["High"].astype(float).max())
        range_low = float(consol_slice["Low"].astype(float).min())

        # ATR contraction: current ATR < ATR from 20 bars ago
        from short_strategies.common.indicators import atr_series
        atr_s = atr_series(df, 14)
        atr_current = float(atr_s.iloc[-1])
        atr_prior = float(atr_s.iloc[-20])
        if pd.isna(atr_current) or pd.isna(atr_prior) or atr_prior <= 0:
            return None
        if atr_current >= atr_prior:
            return None  # no contraction

        # Breakout check
        if direction == "long":
            if price <= range_high:
                return None
            if vol_ratio < cfg.breakout_volume_ratio:
                return None
        else:
            if price >= range_low:
                return None
            if vol_ratio < cfg.breakout_volume_ratio:
                return None

        # Event filter
        if has_upcoming_event(days=cfg.event_blackout_days):
            return None

        # Stop: Chandelier exit
        chandelier_atr = atr(df, cfg.chandelier_atr_period)
        if chandelier_atr is None:
            return None
        if direction == "long":
            highest_high = float(df["High"].astype(float).iloc[-consol_bars:].max())
            stop = highest_high - cfg.chandelier_atr_mult * chandelier_atr
            risk = price - stop
            if risk <= 0:
                return None
            target = price + 3.0 * risk
        else:
            lowest_low = float(df["Low"].astype(float).iloc[-consol_bars:].min())
            stop = lowest_low + cfg.chandelier_atr_mult * chandelier_atr
            risk = stop - price
            if risk <= 0:
                return None
            target = price - 3.0 * risk

        # Strength
        trend_quality = min(abs(sma_slope) / (atr_value * 0.1), 1.0)
        contraction_quality = min((atr_prior - atr_current) / atr_prior, 1.0)
        volume_quality = min(vol_ratio / (2.0 * cfg.breakout_volume_ratio), 1.0)
        strength = min(
            1.0,
            0.25 + 0.25 * trend_quality + 0.25 * contraction_quality + 0.25 * volume_quality,
        )

        sig = SelectiveSignal(
            strategy_id=STRATEGY_ID,
            symbol=symbol,
            signal_strength=round(strength, 4),
            trigger_price=round(price, 4),
            stop_price=round(stop, 4),
            target_price=round(target, 4),
            direction=direction,
            metadata={
                "sma_fast": round(sma_fast_val, 4),
                "sma_slow": round(sma_slow_val, 4),
                "sma_slope": round(sma_slope, 4),
                "range_high": round(range_high, 4),
                "range_low": round(range_low, 4),
                "atr_current": round(atr_current, 4),
                "atr_prior": round(atr_prior, 4),
                "vol_ratio": round(vol_ratio, 4),
            },
        )
        log.info(
            "hs_triple_timeframe.detected",
            symbol=symbol,
            direction=direction,
            vol_ratio=round(vol_ratio, 2),
        )
        return sig
    except Exception:
        log.exception("hs_triple_timeframe.error", symbol=symbol)
        return None
