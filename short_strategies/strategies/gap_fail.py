"""
Strategy 8 — Failed Gap-Up ("Gap and Crap").

Price gaps up over the prior close but the day closes back below the open
(and optionally below the prior close): the opening range failed to hold and
trapped buyers supply the downside fuel.  The day's high is the structural
stop.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
import structlog

from short_strategies.common.config import get_short_config
from short_strategies.common.indicators import atr
from short_strategies.common.signal import ShortSignal
from short_strategies.risk.sizing import short_stop_target

log = structlog.get_logger(__name__)

STRATEGY_ID = "short_gap_fail"


def detect(
    symbol: str,
    df: pd.DataFrame,
    config=None,
    filters=None,
    ctx=None,
) -> Optional[ShortSignal]:
    """Detect a failed gap-up on the last bar.  Never raises."""
    try:
        cfg = config or get_short_config().gap_fail
        fcfg = filters or get_short_config().filters
        if df is None or len(df) < 30:
            return None

        last = df.iloc[-1]
        prior_close = float(df["Close"].astype(float).iloc[-2])
        o = float(last["Open"])
        h = float(last["High"])
        c = float(last["Close"])
        if prior_close <= 0 or c <= 0:
            return None

        gap = o / prior_close - 1.0
        if gap < cfg.gap_min_pct:
            return None
        if cfg.require_below_open and c >= o:
            return None
        if cfg.require_below_prior_close and c >= prior_close:
            return None

        atr_value = atr(df)
        if atr_value is None or atr_value / c < fcfg.min_atr_pct:
            return None

        # Structural stop just above the failed gap day's high.
        structural_stop = h + 0.25 * atr_value
        stop, target = short_stop_target(c, atr_value, fcfg, structural_stop)

        # Strength: gap size plus how much of it was given back.
        giveback = (o - c) / max(o - prior_close, 1e-9)
        strength = min(1.0, 0.40
                       + 0.30 * min(gap / (2 * cfg.gap_min_pct), 1.0)
                       + 0.30 * min(giveback, 1.5) / 1.5)

        sig = ShortSignal(
            strategy_id=STRATEGY_ID,
            symbol=symbol,
            signal_strength=round(strength, 4),
            trigger_price=round(c, 4),
            stop_price=stop,
            target_price=target,
            metadata={
                "gap_pct": round(gap, 4),
                "giveback": round(giveback, 4),
                "day_high": round(h, 4),
                "prior_close": round(prior_close, 4),
            },
        )
        log.info("short_gap_fail.detected", symbol=symbol, gap=round(gap, 3))
        return sig
    except Exception:
        log.exception("short_gap_fail.error", symbol=symbol)
        return None
