"""
Strategy 2 — Moving Average Crossunder.

Fast EMA crosses below slow SMA within the last ``cross_within_bars`` bars
and price closes below both averages.  The reusable trend classifier this
strategy is built on lives in
:func:`short_strategies.common.indicators.trend_state`.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
import structlog

from short_strategies.common.config import get_short_config
from short_strategies.common.indicators import atr, ema, sma
from short_strategies.common.signal import ShortSignal
from short_strategies.risk.sizing import short_stop_target

log = structlog.get_logger(__name__)

STRATEGY_ID = "short_ma_crossunder"


def detect(
    symbol: str,
    df: pd.DataFrame,
    config=None,
    filters=None,
    ctx=None,
) -> Optional[ShortSignal]:
    """Detect a recent fast-EMA/slow-SMA crossunder.  Never raises."""
    try:
        cfg = config or get_short_config().ma_crossunder
        fcfg = filters or get_short_config().filters
        min_rows = cfg.slow_sma_window + cfg.cross_within_bars + 1
        if df is None or len(df) < min_rows:
            return None

        close = df["Close"].astype(float)
        price = float(close.iloc[-1])
        if price <= 0:
            return None
        atr_value = atr(df)
        if atr_value is None or atr_value / price < fcfg.min_atr_pct:
            return None

        fast = ema(close, cfg.fast_ema_span)
        slow = sma(close, cfg.slow_sma_window)
        diff = fast - slow
        if pd.isna(diff.iloc[-1]):
            return None

        # Fast must currently be below slow, and must have been at or above
        # it within the last cross_within_bars bars (a *fresh* crossunder).
        if diff.iloc[-1] >= 0:
            return None
        recent = diff.iloc[-(cfg.cross_within_bars + 1):-1]
        if recent.isna().any() or not (recent >= 0).any():
            return None

        if cfg.require_close_below and not (
            price < float(fast.iloc[-1]) and price < float(slow.iloc[-1])
        ):
            return None

        # Structural stop just above the slow SMA (the reclaimed-trend line).
        structural_stop = float(slow.iloc[-1]) + 0.25 * atr_value
        stop, target = short_stop_target(price, atr_value, fcfg, structural_stop)

        # Strength: separation of the averages plus price distance below.
        separation = abs(float(diff.iloc[-1])) / atr_value
        below_fast = (float(fast.iloc[-1]) - price) / atr_value
        strength = min(1.0, 0.45 + 0.30 * min(separation, 1.0)
                       + 0.25 * min(max(below_fast, 0.0), 1.0))

        sig = ShortSignal(
            strategy_id=STRATEGY_ID,
            symbol=symbol,
            signal_strength=round(strength, 4),
            trigger_price=round(price, 4),
            stop_price=stop,
            target_price=target,
            metadata={
                "fast_ema": round(float(fast.iloc[-1]), 4),
                "slow_sma": round(float(slow.iloc[-1]), 4),
                "separation_atr": round(separation, 4),
            },
        )
        log.info("short_ma_crossunder.detected", symbol=symbol)
        return sig
    except Exception:
        log.exception("short_ma_crossunder.error", symbol=symbol)
        return None
