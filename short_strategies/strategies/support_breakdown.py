"""
Strategy 1 — Support Breakdown with Volume Confirmation.

Short when the daily close breaks below a significant support level (pivot
clustering reused from :mod:`signals.support_resistance`) on above-average
volume.  The broken level, now overhead resistance, provides the structural
stop reference.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
import structlog

from short_strategies.common.config import get_short_config
from short_strategies.common.indicators import atr, volume_ratio
from short_strategies.common.signal import ShortSignal
from short_strategies.risk.sizing import short_stop_target
from signals.support_resistance import cluster_pivots, find_pivots

log = structlog.get_logger(__name__)

STRATEGY_ID = "short_support_breakdown"

_MIN_ROWS = 60


def detect(
    symbol: str,
    df: pd.DataFrame,
    config=None,
    filters=None,
    ctx=None,
) -> Optional[ShortSignal]:
    """Detect a volume-confirmed support breakdown.  Never raises."""
    try:
        cfg = config or get_short_config().support_breakdown
        fcfg = filters or get_short_config().filters
        if df is None or len(df) < _MIN_ROWS:
            return None

        close = df["Close"].astype(float)
        price = float(close.iloc[-1])
        if price <= 0:
            return None
        atr_value = atr(df)
        if atr_value is None or atr_value / price < fcfg.min_atr_pct:
            return None

        # Support levels from pivots strictly BEFORE today, so today's
        # breakdown bar cannot define the level it is breaking.
        window = df.iloc[:-1].tail(cfg.lookback_bars)
        if len(window) < 10:
            return None
        _, lows = find_pivots(window)
        levels = cluster_pivots(lows, tolerance=0.5 * atr_value)
        prior_close = float(close.iloc[-2])
        supports = [
            lv for lv in levels
            if lv["touches"] >= cfg.min_touches and lv["price"] < prior_close
        ]
        if not supports:
            return None
        # Nearest support below the prior close is the one being tested.
        level = max(supports, key=lambda lv: lv["price"])
        support_price = float(level["price"])

        # Breakdown: today closes below support by break_pct, having been
        # above it at the prior close (a fresh break, not an old one).
        if price > support_price * (1.0 - cfg.break_pct):
            return None

        vr = volume_ratio(df)
        if vr is None or vr < cfg.volume_ratio_min:
            return None

        # Structural stop just above the broken level (plus a small buffer).
        structural_stop = support_price + 0.5 * atr_value
        stop, target = short_stop_target(price, atr_value, fcfg, structural_stop)

        break_depth = (support_price - price) / atr_value
        strength = min(1.0, 0.35
                       + 0.30 * min(vr / (2 * cfg.volume_ratio_min), 1.0)
                       + 0.20 * min(level["touches"] / 4.0, 1.0)
                       + 0.15 * min(break_depth, 1.0))

        sig = ShortSignal(
            strategy_id=STRATEGY_ID,
            symbol=symbol,
            signal_strength=round(strength, 4),
            trigger_price=round(price, 4),
            stop_price=stop,
            target_price=target,
            metadata={
                "support_price": round(support_price, 4),
                "touches": int(level["touches"]),
                "volume_ratio": round(vr, 4),
                "break_depth_atr": round(break_depth, 4),
            },
        )
        log.info("short_support_breakdown.detected", symbol=symbol,
                 support=round(support_price, 4), volume_ratio=round(vr, 2))
        return sig
    except Exception:
        log.exception("short_support_breakdown.error", symbol=symbol)
        return None
