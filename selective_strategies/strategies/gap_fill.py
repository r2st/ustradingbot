"""
Strategy E -- Opening Range Gap-Fill (Statistical Gap Fade).

Fades overnight gaps that fall within a statistically favorable size range.
Gap-ups are shorted and gap-downs are bought, targeting the prior day's close
(the full gap fill).  A first-candle indecision filter (proxied on daily data)
and macro event blackout reduce false triggers.

The stop buffer is sized off a real intraday ATR
(:func:`short_strategies.common.indicators.intraday_atr`) when a data fetcher is
injected and the intraday feed is available; otherwise it degrades to the
``daily ATR / divisor`` proxy.  The mode used is logged and recorded in the
signal metadata (``atr_mode``).
"""

from __future__ import annotations

from typing import Callable, Optional

import numpy as np
import pandas as pd
import structlog

from selective_strategies.config import GapFillConfig
from selective_strategies.events import is_macro_event_day
from selective_strategies.signal import SelectiveSignal
from short_strategies.common.indicators import (
    atr,
    ema,
    intraday_atr,
    rsi,
    sma,
    volume_ratio,
)

log = structlog.get_logger(__name__)

STRATEGY_ID = "hs_gap_fill"


def detect(
    symbol: str,
    df: pd.DataFrame,
    config=None,
    filters=None,
    ctx=None,
    *,
    fetch: Optional[Callable[..., Optional[pd.DataFrame]]] = None,
) -> Optional[SelectiveSignal]:
    """Detect gap-fill fade setup on the last bar.  Never raises.

    When *fetch* is injected and ``cfg.use_intraday`` is set, the stop buffer
    uses a real intraday ATR; otherwise it falls back to the ``daily ATR /
    divisor`` proxy.  The mode used is logged and stored in ``metadata``.
    """
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

        # ATR for the stop buffer.  Prefer a real intraday ATR; fall back to the
        # daily ATR / divisor proxy when the intraday feed is unavailable.
        atr_value = atr(df, 14)
        if atr_value is None:
            return None
        intraday_atr_value = None
        atr_mode = "daily_proxy"
        if getattr(cfg, "use_intraday", False) and fetch is not None:
            intraday_atr_value = intraday_atr(
                symbol,
                interval=cfg.intraday_interval,
                period=cfg.intraday_period,
                fetch=fetch,
            )
            if intraday_atr_value is not None:
                atr_mode = "intraday"
        if intraday_atr_value is not None:
            intraday_atr_proxy = intraday_atr_value
        else:
            divisor = getattr(cfg, "daily_atr_intraday_divisor", 5.0) or 5.0
            intraday_atr_proxy = atr_value / divisor

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
                "atr_mode": atr_mode,
                "intraday_atr": round(intraday_atr_proxy, 4),
            },
        )
        log.info(
            "hs_gap_fill.detected",
            symbol=symbol,
            direction=direction,
            gap_pct=round(gap_pct, 4),
            atr_mode=atr_mode,
        )
        return sig
    except Exception:
        log.exception("hs_gap_fill.error", symbol=symbol)
        return None
