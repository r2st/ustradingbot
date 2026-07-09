"""
Strategy E -- Opening Range Gap-Fill (Statistical Gap Fade).

Fades overnight gaps that fall within a statistically favorable size range.
Gap-ups are shorted and gap-downs are bought, targeting the prior day's close
(the full gap fill).  A first-candle indecision filter (proxied on daily data)
and macro event blackout reduce false triggers.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import structlog

from selective_strategies.config import GapFillConfig
from selective_strategies.events import is_macro_event_day
from selective_strategies.signal import SelectiveSignal
from short_strategies.common.indicators import atr, rsi, sma, ema, volume_ratio

log = structlog.get_logger(__name__)

STRATEGY_ID = "hs_gap_fill"


def detect(
    symbol: str,
    df: pd.DataFrame,
    config=None,
    filters=None,
    ctx=None,
) -> Optional[SelectiveSignal]:
    """Detect gap-fill fade setup on the last bar.  Never raises."""
    try:
        cfg = config or GapFillConfig()
        if df is None or len(df) < 20:
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

        # Gap calculation
        gap_pct = (o - prior_close) / prior_close

        # Macro event filter
        if is_macro_event_day():
            return None

        # Determine if gap is in acceptable range and set direction
        direction = None
        if cfg.gap_min_pct <= gap_pct <= cfg.gap_max_pct:
            # Gap-up: fade by going SHORT
            direction = "short"
        elif -cfg.gap_max_pct <= gap_pct <= -cfg.gap_min_pct:
            # Gap-down: fade by going LONG
            direction = "long"
        else:
            return None

        # First candle indecision (proxy on daily data)
        day_range = h - l
        if day_range <= 0:
            return None
        body_pct = abs(c - o) / day_range
        if body_pct >= cfg.max_body_pct:
            return None

        # ATR for stop buffer (use daily ATR / 5 as 5-min proxy)
        atr_value = atr(df, 14)
        if atr_value is None:
            return None
        intraday_atr_proxy = atr_value / 5.0

        # Target: prior day's close (the gap fill)
        target = prior_close

        # Stop
        if direction == "short":
            # Gap-up fade: stop above day high
            stop = h + cfg.stop_buffer_atr_mult * intraday_atr_proxy
        else:
            # Gap-down fade: stop below day low
            stop = l - cfg.stop_buffer_atr_mult * intraday_atr_proxy

        # Strength
        gap_midpoint = (cfg.gap_min_pct + cfg.gap_max_pct) / 2.0
        gap_range_width = (cfg.gap_max_pct - cfg.gap_min_pct) / 2.0
        gap_quality = 1.0 - abs(abs(gap_pct) - gap_midpoint) / gap_range_width if gap_range_width > 0 else 0.5
        gap_quality = max(0.0, min(1.0, gap_quality))
        indecision_quality = 1.0 - body_pct / cfg.max_body_pct if cfg.max_body_pct > 0 else 0.0

        strength = min(
            1.0,
            0.35 + 0.30 * gap_quality + 0.35 * indecision_quality,
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
                "body_pct": round(body_pct, 4),
                "prior_close": round(prior_close, 4),
                "gap_quality": round(gap_quality, 4),
                "indecision_quality": round(indecision_quality, 4),
                "atr14": round(atr_value, 4),
            },
        )
        log.info(
            "hs_gap_fill.detected",
            symbol=symbol,
            direction=direction,
            gap_pct=round(gap_pct, 4),
        )
        return sig
    except Exception:
        log.exception("hs_gap_fill.error", symbol=symbol)
        return None
