"""
Strategy A -- RSI-2 Extreme Mean Reversion at Structural Support/Resistance.

Enters when the 2-period RSI reaches an extreme reading (below 5 for longs,
above 95 for shorts) while price is near a confirmed support or resistance
level that has been touched multiple times over the lookback window.  The
structural level plus volume confirmation produces a high-probability
mean-reversion setup.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import structlog

from selective_strategies.config import RSI2ReversalConfig
from selective_strategies.signal import SelectiveSignal
from short_strategies.common.indicators import atr, rsi, sma, ema, volume_ratio

log = structlog.get_logger(__name__)

STRATEGY_ID = "hs_rsi2_reversal"


def _find_support_resistance(df, lookback_days, proximity_pct):
    """Find support and resistance levels using fractal pivots."""
    recent = df.tail(lookback_days)
    if len(recent) < 10:
        return [], []

    highs = recent["High"].values.astype(float)
    lows = recent["Low"].values.astype(float)
    closes = recent["Close"].values.astype(float)

    supports = []
    resistances = []

    for i in range(2, len(recent) - 2):
        # Support: low is lower than 2 bars on each side
        if lows[i] <= min(lows[i - 1], lows[i - 2], lows[i + 1], lows[i + 2]):
            level = lows[i]
            touches = sum(
                1 for c in closes if abs(c - level) / level < proximity_pct
            )
            supports.append({"level": level, "touches": touches})
        # Resistance: high is higher than 2 bars on each side
        if highs[i] >= max(highs[i - 1], highs[i - 2], highs[i + 1], highs[i + 2]):
            level = highs[i]
            touches = sum(
                1 for c in closes if abs(c - level) / level < proximity_pct
            )
            resistances.append({"level": level, "touches": touches})

    return supports, resistances


def detect(
    symbol: str,
    df: pd.DataFrame,
    config=None,
    filters=None,
    ctx=None,
) -> Optional[SelectiveSignal]:
    """Detect RSI-2 extreme at structural support/resistance.  Never raises."""
    try:
        cfg = config or RSI2ReversalConfig()
        if df is None or len(df) < 200:
            return None

        close = df["Close"].astype(float)
        price = float(close.iloc[-1])
        if price <= 0:
            return None

        rsi_val = rsi(close, period=2)
        sma_200 = sma(close, 200)
        sma_5 = sma(close, 5)
        atr_value = atr(df, 14)
        vol_ratio = volume_ratio(df, 20)

        if atr_value is None or vol_ratio is None:
            return None

        sma_200_val = float(sma_200.iloc[-1])
        sma_5_val = float(sma_5.iloc[-1])
        if pd.isna(sma_200_val) or pd.isna(sma_5_val):
            return None

        supports, resistances = _find_support_resistance(
            df, cfg.support_lookback_days, cfg.support_proximity_pct
        )

        direction = None
        touches = 0
        nearest_level = None

        # --- LONG entry check ---
        if price > sma_200_val and rsi_val < cfg.rsi_threshold:
            # Check if near a support level with enough touches
            for s in supports:
                if (
                    s["touches"] >= cfg.min_support_touches
                    and abs(price - s["level"]) / s["level"] < cfg.support_proximity_pct
                ):
                    if nearest_level is None or s["touches"] > touches:
                        nearest_level = s["level"]
                        touches = s["touches"]
            if nearest_level is not None and vol_ratio >= cfg.volume_ratio_min:
                direction = "long"

        # --- SHORT entry check (mirror) ---
        if direction is None and price < sma_200_val and rsi_val > (100 - cfg.rsi_threshold):
            for r in resistances:
                if (
                    r["touches"] >= cfg.min_support_touches
                    and abs(price - r["level"]) / r["level"] < cfg.support_proximity_pct
                ):
                    if nearest_level is None or r["touches"] > touches:
                        nearest_level = r["level"]
                        touches = r["touches"]
            if nearest_level is not None and vol_ratio >= cfg.volume_ratio_min:
                direction = "short"

        if direction is None:
            return None

        # Stop and target
        if direction == "long":
            stop = price - cfg.stop_atr_mult * atr_value
            target = sma_5_val
            rsi_extremity = min((cfg.rsi_threshold - rsi_val) / cfg.rsi_threshold, 1.0)
        else:
            stop = price + cfg.stop_atr_mult * atr_value
            target = sma_5_val
            rsi_extremity = min(
                (rsi_val - (100 - cfg.rsi_threshold)) / cfg.rsi_threshold, 1.0
            )

        volume_quality = min(vol_ratio / 2.0, 1.0)
        touch_quality = min(touches / 4, 1.0)
        strength = min(
            1.0,
            0.30 + 0.25 * rsi_extremity + 0.25 * volume_quality + 0.20 * touch_quality,
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
                "rsi2": round(rsi_val, 4),
                "sma200": round(sma_200_val, 4),
                "sma5": round(sma_5_val, 4),
                "atr14": round(atr_value, 4),
                "vol_ratio": round(vol_ratio, 4),
                "nearest_level": round(nearest_level, 4),
                "touches": touches,
                "rsi_extremity": round(rsi_extremity, 4),
            },
        )
        log.info(
            "hs_rsi2_reversal.detected",
            symbol=symbol,
            direction=direction,
            rsi2=round(rsi_val, 2),
        )
        return sig
    except Exception:
        log.exception("hs_rsi2_reversal.error", symbol=symbol)
        return None
