"""
Strategy 3 — Bear Flag Continuation.

A sharp decline (the flagpole) is followed by a short, low-momentum
consolidation drifting sideways/up (the flag).  Short on the close breaking
below the flag low — continuation of the pole.  The flag high provides the
structural stop.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import structlog

from short_strategies.common.config import get_short_config
from short_strategies.common.indicators import atr
from short_strategies.common.signal import ShortSignal
from short_strategies.risk.sizing import short_stop_target

log = structlog.get_logger(__name__)

STRATEGY_ID = "short_bear_flag"


def detect(
    symbol: str,
    df: pd.DataFrame,
    config=None,
    filters=None,
    ctx=None,
) -> Optional[ShortSignal]:
    """Detect a bear-flag breakdown.  Never raises."""
    try:
        cfg = config or get_short_config().bear_flag
        fcfg = filters or get_short_config().filters
        min_rows = cfg.pole_max_bars + cfg.flag_max_bars + 5
        if df is None or len(df) < min_rows:
            return None

        close = df["Close"].astype(float)
        price = float(close.iloc[-1])
        if price <= 0:
            return None
        atr_value = atr(df)
        if atr_value is None or atr_value / price < fcfg.min_atr_pct:
            return None

        # Try each admissible flag length; today's bar is the breakdown bar,
        # so the flag body is the flag_bars bars before it.
        for flag_bars in range(cfg.flag_min_bars, cfg.flag_max_bars + 1):
            flag = df.iloc[-(flag_bars + 1):-1]
            pole = df.iloc[-(flag_bars + 1 + cfg.pole_max_bars):-(flag_bars + 1)]
            if len(flag) < cfg.flag_min_bars or len(pole) < 3:
                continue

            # Flagpole: a decline of at least pole_min_drop_pct from the
            # pole's high to its low.
            pole_high = float(pole["High"].astype(float).max())
            pole_low = float(pole["Low"].astype(float).min())
            if pole_high <= 0:
                continue
            pole_drop = (pole_high - pole_low) / pole_high
            if pole_drop < cfg.pole_min_drop_pct:
                continue
            # The pole must actually end low (downward move, not a V-shape).
            if float(pole["Close"].astype(float).iloc[-1]) > pole_high - 0.5 * (pole_high - pole_low):
                continue

            flag_high = float(flag["High"].astype(float).max())
            flag_low = float(flag["Low"].astype(float).min())

            # Flag retraces at most flag_max_retrace of the pole.
            pole_range = pole_high - pole_low
            if pole_range <= 0:
                continue
            retrace = (flag_high - pole_low) / pole_range
            if retrace > cfg.flag_max_retrace:
                continue

            # Flag drifts sideways/up: its closes do not make new lows below
            # the pole low.
            if float(flag["Close"].astype(float).min()) < pole_low:
                continue

            # Breakdown: today closes below the flag low.
            if price >= flag_low * (1.0 - cfg.break_pct):
                continue

            structural_stop = flag_high + 0.25 * atr_value
            stop, target = short_stop_target(price, atr_value, fcfg, structural_stop)

            tightness = np.clip(1.0 - retrace / cfg.flag_max_retrace, 0.0, 1.0)
            pole_quality = min(pole_drop / (2 * cfg.pole_min_drop_pct), 1.0)
            strength = float(min(1.0, 0.40 + 0.30 * pole_quality + 0.30 * tightness))

            sig = ShortSignal(
                strategy_id=STRATEGY_ID,
                symbol=symbol,
                signal_strength=round(strength, 4),
                trigger_price=round(price, 4),
                stop_price=stop,
                target_price=target,
                metadata={
                    "pole_drop_pct": round(pole_drop, 4),
                    "flag_bars": flag_bars,
                    "flag_low": round(flag_low, 4),
                    "flag_high": round(flag_high, 4),
                    "retrace": round(retrace, 4),
                },
            )
            log.info("short_bear_flag.detected", symbol=symbol,
                     flag_bars=flag_bars, pole_drop=round(pole_drop, 3))
            return sig
        return None
    except Exception:
        log.exception("short_bear_flag.error", symbol=symbol)
        return None
