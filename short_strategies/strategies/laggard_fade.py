"""
Strategy 6 — Laggard Fade on Market Pullback Days.

On a day the benchmark falls at least ``market_down_pct``, short the symbols
that underperform it by ``underperform_pct`` or more and close near the
bottom of their day range (no dip-buyers showed up).  Requires the
scanner-supplied :class:`~short_strategies.common.context.MarketContext`.
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

STRATEGY_ID = "short_laggard_fade"


def detect(
    symbol: str,
    df: pd.DataFrame,
    config=None,
    filters=None,
    ctx=None,
) -> Optional[ShortSignal]:
    """Detect a laggard on a market pullback day.  Never raises."""
    try:
        cfg = config or get_short_config().laggard_fade
        fcfg = filters or get_short_config().filters
        if ctx is None or df is None or len(df) < 30:
            return None

        bench_ret = ctx.benchmark_day_return
        if bench_ret is None or bench_ret > -cfg.market_down_pct:
            return None
        my_ret = ctx.day_returns.get(symbol)
        if my_ret is None or (bench_ret - my_ret) < cfg.underperform_pct:
            return None

        last = df.iloc[-1]
        price = float(last["Close"])
        day_high = float(last["High"])
        day_low = float(last["Low"])
        if price <= 0 or day_high <= day_low:
            return None
        close_pos = (price - day_low) / (day_high - day_low)
        if close_pos > cfg.close_range_max:
            return None

        atr_value = atr(df)
        if atr_value is None or atr_value / price < fcfg.min_atr_pct:
            return None

        # Structural stop just above the day's high (the failed rally point).
        structural_stop = day_high + 0.25 * atr_value
        stop, target = short_stop_target(price, atr_value, fcfg, structural_stop)

        underperf = bench_ret - my_ret
        strength = min(1.0, 0.40
                       + 0.35 * min(underperf / (3 * cfg.underperform_pct), 1.0)
                       + 0.25 * (1.0 - close_pos))

        sig = ShortSignal(
            strategy_id=STRATEGY_ID,
            symbol=symbol,
            signal_strength=round(strength, 4),
            trigger_price=round(price, 4),
            stop_price=stop,
            target_price=target,
            metadata={
                "benchmark_day_return": round(bench_ret, 4),
                "day_return": round(my_ret, 4),
                "underperformance": round(underperf, 4),
                "close_range_position": round(close_pos, 4),
            },
        )
        log.info("short_laggard_fade.detected", symbol=symbol,
                 underperf=round(underperf, 4))
        return sig
    except Exception:
        log.exception("short_laggard_fade.error", symbol=symbol)
        return None
