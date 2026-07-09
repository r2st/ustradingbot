"""
Strategy D -- Post-Earnings Volume-Confirmed Drift Continuation.

Detects a recent earnings gap (today's open vs yesterday's close >=3%) with
volume confirmation and strong close position.  Trades the well-documented
Post-Earnings Announcement Drift (PEAD) effect: prices continue to drift in
the direction of the initial earnings surprise for days to weeks.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import structlog

from selective_strategies.config import PEADDriftConfig
from selective_strategies.signal import SelectiveSignal
from short_strategies.common.indicators import atr, rsi, sma, ema, volume_ratio

log = structlog.get_logger(__name__)

STRATEGY_ID = "hs_pead_drift"


def detect(
    symbol: str,
    df: pd.DataFrame,
    config=None,
    filters=None,
    ctx=None,
) -> Optional[SelectiveSignal]:
    """Detect post-earnings drift setup on the last bar.  Never raises."""
    try:
        cfg = config or PEADDriftConfig()
        if df is None or len(df) < 30:
            return None

        close = df["Close"].astype(float)
        price = float(close.iloc[-1])
        if price <= 0:
            return None

        last = df.iloc[-1]
        o = float(last["Open"])
        h = float(last["High"])
        l = float(last["Low"])
        c = price

        prior_close = float(close.iloc[-2])
        if prior_close <= 0:
            return None

        # Gap detection: compare today's open vs yesterday's close
        gap_pct = (o - prior_close) / prior_close
        if abs(gap_pct) < cfg.min_gap_pct:
            return None

        # Volume check
        vol_ratio = volume_ratio(df, 20)
        if vol_ratio is None or vol_ratio < cfg.min_volume_ratio:
            return None

        # Close in top third of day's range
        day_range = h - l
        if day_range <= 0:
            return None
        range_position = (c - l) / day_range
        if range_position < cfg.min_close_range_pct:
            return None

        # Close must be >= open (held above open)
        if c < o:
            return None

        # Direction based on gap
        if gap_pct > 0:
            direction = "long"
        else:
            direction = "short"

        # ATR for stop
        atr_value = atr(df, 14)
        if atr_value is None:
            return None

        # Stop: 2x ATR from entry (wide stop for drift trade)
        if direction == "long":
            stop = c - cfg.stop_atr_mult * atr_value
            risk = c - stop
            if risk <= 0:
                return None
            target = c + 3.0 * risk
        else:
            stop = c + cfg.stop_atr_mult * atr_value
            risk = stop - c
            if risk <= 0:
                return None
            target = c - 3.0 * risk

        # Strength
        gap_quality = min(abs(gap_pct) / 0.10, 1.0)
        volume_quality = min(vol_ratio / 4.0, 1.0)
        close_quality = range_position

        strength = min(
            1.0,
            0.30 + 0.25 * gap_quality + 0.25 * volume_quality + 0.20 * close_quality,
        )

        sig = SelectiveSignal(
            strategy_id=STRATEGY_ID,
            symbol=symbol,
            signal_strength=round(strength, 4),
            trigger_price=round(c, 4),
            stop_price=round(stop, 4),
            target_price=round(target, 4),
            direction=direction,
            metadata={
                "gap_pct": round(gap_pct, 4),
                "vol_ratio": round(vol_ratio, 4),
                "range_position": round(range_position, 4),
                "hold_days": cfg.hold_days,
                "atr14": round(atr_value, 4),
                "prior_close": round(prior_close, 4),
            },
        )
        log.info(
            "hs_pead_drift.detected",
            symbol=symbol,
            direction=direction,
            gap_pct=round(gap_pct, 3),
        )
        return sig
    except Exception:
        log.exception("hs_pead_drift.error", symbol=symbol)
        return None
