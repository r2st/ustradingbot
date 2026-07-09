"""
Strategy F -- Turnaround Tuesday.

A well-documented calendar anomaly: when the market drops on Monday, it tends
to reverse on Tuesday.  This detector fires on Monday when the session closes
at least 1% below Friday's close with a low IBS (Internal Bar Strength),
indicating the market closed near its low and is primed for a bounce.

LONG only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import structlog

from config.settings import EASTERN
from selective_strategies.config import TurnaroundTuesdayConfig
from selective_strategies.signal import SelectiveSignal
from short_strategies.common.indicators import atr, rsi, sma, ema, volume_ratio

log = structlog.get_logger(__name__)

STRATEGY_ID = "hs_turnaround_tuesday"


def detect(
    symbol: str,
    df: pd.DataFrame,
    config=None,
    filters=None,
    ctx=None,
) -> Optional[SelectiveSignal]:
    """Detect Turnaround Tuesday setup on the last bar.  Never raises."""
    try:
        cfg = config or TurnaroundTuesdayConfig()
        if df is None or len(df) < 10:
            return None

        # Check if today is Monday (weekday 0) in Eastern timezone
        now_eastern = datetime.now(tz=EASTERN)
        if now_eastern.weekday() != 0:
            return None

        close = df["Close"].astype(float)
        price = float(close.iloc[-1])
        if price <= 0:
            return None

        last = df.iloc[-1]
        h = float(last["High"])
        l = float(last["Low"])
        c = price

        # Get prior Friday's close: look back for the most recent bar before today
        if len(df) < 2:
            return None
        prior_friday_close = float(close.iloc[-2])
        if prior_friday_close <= 0:
            return None

        # Monday drop check: today's close at least min_monday_drop_pct below Friday
        drop_pct = (prior_friday_close - c) / prior_friday_close
        if drop_pct < cfg.min_monday_drop_pct:
            return None

        # IBS check: Internal Bar Strength
        day_range = h - l
        if day_range <= 0:
            return None
        ibs_val = (c - l) / day_range
        if ibs_val >= cfg.max_ibs:
            return None

        # Trend filter (optional)
        trend_quality = 0.5  # default: neutral if no filter
        if cfg.require_trend_filter:
            if len(df) < cfg.sma_period:
                return None
            sma_val = float(sma(close, cfg.sma_period).iloc[-1])
            if pd.isna(sma_val):
                return None
            if c <= sma_val:
                return None
            trend_quality = 1.0
        else:
            # Even without the filter, compute trend quality for strength
            if len(df) >= cfg.sma_period:
                sma_val = float(sma(close, cfg.sma_period).iloc[-1])
                if not pd.isna(sma_val):
                    trend_quality = 1.0 if c > sma_val else 0.5

        # Direction: LONG only
        direction = "long"

        # Entry: Monday's close
        entry = c

        # Stop: hard stop below entry
        stop = entry * (1.0 - cfg.hard_stop_pct)

        # Target: modest 1% for a 1-day hold
        target = entry * 1.01

        # Strength
        drop_quality = min(drop_pct / 0.03, 1.0)
        ibs_quality = 1.0 - ibs_val / cfg.max_ibs if cfg.max_ibs > 0 else 0.0

        strength = min(
            1.0,
            0.35 + 0.25 * drop_quality + 0.25 * ibs_quality + 0.15 * trend_quality,
        )

        sig = SelectiveSignal(
            strategy_id=STRATEGY_ID,
            symbol=symbol,
            signal_strength=round(strength, 4),
            trigger_price=round(entry, 4),
            stop_price=round(stop, 4),
            target_price=round(target, 4),
            direction=direction,
            timestamp=now_eastern,
            metadata={
                "prior_friday_close": round(prior_friday_close, 4),
                "drop_pct": round(drop_pct, 4),
                "ibs": round(ibs_val, 4),
                "trend_quality": round(trend_quality, 4),
                "hold_days": 1,
            },
        )
        log.info(
            "hs_turnaround_tuesday.detected",
            symbol=symbol,
            drop_pct=round(drop_pct, 3),
            ibs=round(ibs_val, 3),
        )
        return sig
    except Exception:
        log.exception("hs_turnaround_tuesday.error", symbol=symbol)
        return None
