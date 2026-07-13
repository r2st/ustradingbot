"""
Strategy 10 — VWAP Rejection Short.

In an established downtrend, a relief bounce rallies into the volume-weighted
average price and fails there: the high tags the VWAP but the close finishes
back below it.

The VWAP is computed from real intraday bars
(:func:`short_strategies.common.indicators.intraday_vwap`) when a data fetcher
is injected and the intraday feed is available; otherwise it degrades to the
daily typical-price approximation
(:func:`short_strategies.common.indicators.rolling_vwap`).  The mode actually
used is logged and recorded in the signal metadata (``vwap_mode``).
"""

from __future__ import annotations

from typing import Callable, Optional

import pandas as pd
import structlog

from short_strategies.common.config import get_short_config
from short_strategies.common.indicators import (
    atr,
    intraday_vwap,
    rolling_vwap,
    trend_state,
)
from short_strategies.common.signal import ShortSignal
from short_strategies.risk.sizing import short_stop_target

log = structlog.get_logger(__name__)

STRATEGY_ID = "short_vwap_rejection"


def detect(
    symbol: str,
    df: pd.DataFrame,
    config=None,
    filters=None,
    ctx=None,
    *,
    fetch: Optional[Callable[..., Optional[pd.DataFrame]]] = None,
) -> Optional[ShortSignal]:
    """Detect a failed bounce at the VWAP in a downtrend.  Never raises.

    When *fetch* is injected and ``cfg.use_intraday`` is set, the VWAP comes
    from real intraday bars; otherwise it falls back to the daily rolling-VWAP
    approximation.  The mode used is logged and stored in ``metadata``.
    """
    try:
        cfg = config or get_short_config().vwap_rejection
        fcfg = filters or get_short_config().filters
        if df is None or len(df) < cfg.slow_sma_window + 1:
            return None

        # Established downtrend required (reusable trend_state utility).
        if trend_state(df, cfg.fast_ema_span, cfg.slow_sma_window) != "down":
            return None

        # Prefer a true intraday VWAP; fall back to the daily approximation.
        vwap = None
        vwap_mode = "daily"
        if getattr(cfg, "use_intraday", False) and fetch is not None:
            vwap = intraday_vwap(
                symbol,
                interval=cfg.intraday_interval,
                period=cfg.intraday_period,
                fetch=fetch,
            )
            if vwap is not None:
                vwap_mode = "intraday"
        if vwap is None:
            vwap = rolling_vwap(df, cfg.vwap_window)
            vwap_mode = "daily"
        if vwap is None:
            return None

        last = df.iloc[-1]
        h = float(last["High"])
        c = float(last["Close"])
        if c <= 0:
            return None

        # The bounce tagged the VWAP but closed back below it.
        if h < vwap:
            return None
        if c > vwap * (1.0 - cfg.reject_close_pct):
            return None

        atr_value = atr(df)
        if atr_value is None or atr_value / c < fcfg.min_atr_pct:
            return None

        # Structural stop just above the VWAP / rejection high.
        structural_stop = max(vwap, h) + 0.25 * atr_value
        stop, target = short_stop_target(c, atr_value, fcfg, structural_stop)

        rejection_depth = (vwap - c) / atr_value
        strength = min(1.0, 0.45 + 0.35 * min(rejection_depth, 1.0)
                       + 0.20 * min((h - vwap) / atr_value, 1.0))

        sig = ShortSignal(
            strategy_id=STRATEGY_ID,
            symbol=symbol,
            signal_strength=round(strength, 4),
            trigger_price=round(c, 4),
            stop_price=stop,
            target_price=target,
            metadata={
                "vwap": round(vwap, 4),
                "vwap_mode": vwap_mode,
                "rejection_high": round(h, 4),
                "rejection_depth_atr": round(rejection_depth, 4),
                "vwap_window": cfg.vwap_window,
            },
        )
        log.info("short_vwap_rejection.detected", symbol=symbol,
                 vwap=round(vwap, 2), vwap_mode=vwap_mode)
        return sig
    except Exception:
        log.exception("short_vwap_rejection.error", symbol=symbol)
        return None
