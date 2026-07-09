"""
Strategy C -- Bollinger Band Climax Reversal with Volume Exhaustion.

Enters when price closes beyond a Bollinger Band on extreme volume (top
percentile) with an accelerating move into the band, suggesting exhaustion.
A confirming candle pattern (hammer or bullish engulfing for longs) adds
conviction.  Target is the middle Bollinger Band (20-SMA mean reversion).
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import structlog

from selective_strategies.config import BBClimaxConfig
from selective_strategies.signal import SelectiveSignal
from short_strategies.common.indicators import atr, rsi, sma, ema, volume_ratio

log = structlog.get_logger(__name__)

STRATEGY_ID = "hs_bb_climax"


def _is_hammer(o, h, l, c):
    """Check if the candle is a hammer (bullish reversal)."""
    body = abs(c - o)
    full_range = h - l
    if full_range <= 0:
        return False
    lower_shadow = min(o, c) - l
    upper_shadow = h - max(o, c)
    return lower_shadow >= 2 * body and upper_shadow < body


def _is_bullish_engulfing(prev_o, prev_c, cur_o, cur_c):
    """Check if the current bar engulfs the previous bearish bar."""
    prev_bearish = prev_c < prev_o
    cur_bullish = cur_c > cur_o
    engulfs = cur_c > prev_o and cur_o < prev_c
    return prev_bearish and cur_bullish and engulfs


def _is_shooting_star(o, h, l, c):
    """Check if the candle is a shooting star (bearish reversal)."""
    body = abs(c - o)
    full_range = h - l
    if full_range <= 0:
        return False
    upper_shadow = h - max(o, c)
    lower_shadow = min(o, c) - l
    return upper_shadow >= 2 * body and lower_shadow < body


def _is_bearish_engulfing(prev_o, prev_c, cur_o, cur_c):
    """Check if the current bar bearishly engulfs the previous bullish bar."""
    prev_bullish = prev_c > prev_o
    cur_bearish = cur_c < cur_o
    engulfs = cur_o > prev_c and cur_c < prev_o
    return prev_bullish and cur_bearish and engulfs


def detect(
    symbol: str,
    df: pd.DataFrame,
    config=None,
    filters=None,
    ctx=None,
) -> Optional[SelectiveSignal]:
    """Detect BB climax reversal on the last bar.  Never raises."""
    try:
        cfg = config or BBClimaxConfig()
        if df is None or len(df) < max(100, cfg.volume_lookback + 1):
            return None

        close = df["Close"].astype(float)
        price = float(close.iloc[-1])
        if price <= 0:
            return None

        # Bollinger Bands
        middle_bb = sma(close, cfg.bb_period)
        rolling_std = close.rolling(window=cfg.bb_period).std()
        middle_val = float(middle_bb.iloc[-1])
        std_val = float(rolling_std.iloc[-1])
        if pd.isna(middle_val) or pd.isna(std_val) or std_val <= 0:
            return None

        upper_bb = middle_val + cfg.bb_std * std_val
        lower_bb = middle_val - cfg.bb_std * std_val

        # Determine direction based on BB breach
        direction = None
        if price < lower_bb:
            direction = "long"
        elif price > upper_bb:
            direction = "short"
        else:
            return None

        # Volume percentile check
        vol = df["Volume"].astype(float)
        vol_lookback = vol.iloc[-cfg.volume_lookback:]
        today_vol = float(vol.iloc[-1])
        vol_percentile = float(
            (vol_lookback < today_vol).sum() / len(vol_lookback) * 100
        )
        if vol_percentile < cfg.volume_percentile_threshold:
            return None

        # Candle pattern recognition
        last = df.iloc[-1]
        prev = df.iloc[-2]
        o = float(last["Open"])
        h = float(last["High"])
        l = float(last["Low"])
        c = price
        prev_o = float(prev["Open"])
        prev_c = float(prev["Close"])

        has_reversal_pattern = False
        if direction == "long":
            has_reversal_pattern = _is_hammer(o, h, l, c) or _is_bullish_engulfing(
                prev_o, prev_c, o, c
            )
        else:
            has_reversal_pattern = _is_shooting_star(o, h, l, c) or _is_bearish_engulfing(
                prev_o, prev_c, o, c
            )

        # Acceleration check: last N closes strictly decreasing (long) or increasing (short)
        accel_closes = close.iloc[-cfg.accel_days:].values
        if direction == "long":
            accel_ok = all(
                accel_closes[i] < accel_closes[i - 1]
                for i in range(1, len(accel_closes))
            )
        else:
            accel_ok = all(
                accel_closes[i] > accel_closes[i - 1]
                for i in range(1, len(accel_closes))
            )
        if not accel_ok:
            return None

        # Stop and target
        atr_value = atr(df, 14)
        if atr_value is None:
            return None

        if direction == "long":
            stop = l - cfg.stop_atr_mult * atr_value
            target = middle_val
        else:
            stop = h + cfg.stop_atr_mult * atr_value
            target = middle_val

        # Strength
        vol_percentile_quality = vol_percentile / 100.0
        if direction == "long":
            bb_stretch = min((lower_bb - price) / std_val, 1.0) if std_val > 0 else 0
        else:
            bb_stretch = min((price - upper_bb) / std_val, 1.0) if std_val > 0 else 0
        accel_quality = 1.0 if accel_ok else 0.0

        strength = min(
            1.0,
            0.30
            + 0.30 * vol_percentile_quality
            + 0.20 * bb_stretch
            + 0.20 * accel_quality,
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
                "upper_bb": round(upper_bb, 4),
                "lower_bb": round(lower_bb, 4),
                "middle_bb": round(middle_val, 4),
                "vol_percentile": round(vol_percentile, 2),
                "has_reversal_pattern": has_reversal_pattern,
                "accel_ok": accel_ok,
                "bb_stretch": round(bb_stretch, 4),
                "atr14": round(atr_value, 4),
            },
        )
        log.info(
            "hs_bb_climax.detected",
            symbol=symbol,
            direction=direction,
            vol_pctile=round(vol_percentile, 1),
        )
        return sig
    except Exception:
        log.exception("hs_bb_climax.error", symbol=symbol)
        return None
