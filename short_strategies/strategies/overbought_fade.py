"""
Strategy 7 — Overbought Momentum Fade.

RSI at an extreme plus a rejection candle (long upper wick, weak close) at or
near clustered resistance.  A counter-trend fade, so the stop is the
rejection high — tight and mechanical.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
import structlog

from short_strategies.common.config import get_short_config
from short_strategies.common.indicators import atr, rsi
from short_strategies.common.signal import ShortSignal
from short_strategies.risk.sizing import short_stop_target
from signals.support_resistance import cluster_pivots, find_pivots

log = structlog.get_logger(__name__)

STRATEGY_ID = "short_overbought_fade"

_MIN_ROWS = 60


def detect(
    symbol: str,
    df: pd.DataFrame,
    config=None,
    filters=None,
    ctx=None,
) -> Optional[ShortSignal]:
    """Detect an overbought rejection at resistance.  Never raises."""
    try:
        cfg = config or get_short_config().overbought_fade
        fcfg = filters or get_short_config().filters
        if df is None or len(df) < _MIN_ROWS:
            return None

        close = df["Close"].astype(float)
        price = float(close.iloc[-1])
        if price <= 0:
            return None

        rsi_value = rsi(close)
        if rsi_value < cfg.rsi_min:
            return None

        last = df.iloc[-1]
        o = float(last["Open"])
        h = float(last["High"])
        low_ = float(last["Low"])
        c = float(last["Close"])
        day_range = h - low_
        if day_range <= 0:
            return None

        # Rejection candle: long upper wick relative to the body, close in
        # the lower part of the range.
        body = abs(c - o)
        upper_wick = h - max(c, o)
        if body > 0 and upper_wick / body < cfg.wick_body_ratio:
            return None
        if body == 0 and upper_wick < 0.5 * day_range:
            return None
        close_pos = (c - low_) / day_range
        if close_pos > cfg.close_range_max:
            return None

        atr_value = atr(df)
        if atr_value is None or atr_value / price < fcfg.min_atr_pct:
            return None

        # Resistance proximity: the rejection high must have tagged a level.
        if cfg.resistance_proximity_pct > 0:
            window = df.iloc[:-1].tail(120)
            highs, _ = find_pivots(window)
            levels = cluster_pivots(highs, tolerance=0.5 * atr_value)
            near = [
                lv for lv in levels
                if abs(h - lv["price"]) / h <= cfg.resistance_proximity_pct
            ]
            if not near:
                return None

        # Structural stop just above the rejection high.
        structural_stop = h + 0.25 * atr_value
        stop, target = short_stop_target(price, atr_value, fcfg, structural_stop)

        strength = min(1.0, 0.40
                       + 0.30 * min((rsi_value - cfg.rsi_min) / 15.0, 1.0)
                       + 0.30 * (1.0 - close_pos))

        sig = ShortSignal(
            strategy_id=STRATEGY_ID,
            symbol=symbol,
            signal_strength=round(strength, 4),
            trigger_price=round(price, 4),
            stop_price=stop,
            target_price=target,
            metadata={
                "rsi": round(rsi_value, 2),
                "upper_wick": round(upper_wick, 4),
                "close_range_position": round(close_pos, 4),
                "rejection_high": round(h, 4),
            },
        )
        log.info("short_overbought_fade.detected", symbol=symbol,
                 rsi=round(rsi_value, 1))
        return sig
    except Exception:
        log.exception("short_overbought_fade.error", symbol=symbol)
        return None
