"""
Strategy 4 — ADX Trend-Strength Confirmation.

Primarily a *confirmation filter*: a short in a supposed downtrend is only
taken when Wilder ADX(14) >= ``adx_min`` with -DI > +DI (the downtrend has
measurable strength).  The scanner applies :func:`confirms_downtrend` to the
trend-following strategies when ``SHORT_ADX_CONFIRM_ENABLED`` is on.

A standalone detector mode exists (short on ADX-confirmed weakness below the
slow SMA) but is off by default per the spec's priority table.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
import structlog

from short_strategies.common.config import get_short_config
from short_strategies.common.indicators import adx, atr, sma
from short_strategies.common.signal import ShortSignal
from short_strategies.risk.sizing import short_stop_target

log = structlog.get_logger(__name__)

STRATEGY_ID = "short_adx_filter"


def confirms_downtrend(df: pd.DataFrame, config=None) -> bool:
    """Whether ADX confirms a tradeable downtrend (spec: ADX>=20, -DI>+DI).

    Fails closed: insufficient data means no confirmation.
    """
    cfg = config or get_short_config().adx_filter
    values = adx(df, cfg.period)
    if values is None:
        return False
    return values["adx"] >= cfg.adx_min and values["minus_di"] > values["plus_di"]


def detect(
    symbol: str,
    df: pd.DataFrame,
    config=None,
    filters=None,
    ctx=None,
) -> Optional[ShortSignal]:
    """Standalone ADX-weakness short (disabled by default).  Never raises."""
    try:
        cfg = config or get_short_config().adx_filter
        if not cfg.standalone_enabled:
            return None
        fcfg = filters or get_short_config().filters
        if df is None or len(df) < 60:
            return None
        values = adx(df, cfg.period)
        if values is None or not confirms_downtrend(df, cfg):
            return None

        close = df["Close"].astype(float)
        price = float(close.iloc[-1])
        slow = sma(close, 50)
        if price <= 0 or pd.isna(slow.iloc[-1]) or price >= float(slow.iloc[-1]):
            return None
        atr_value = atr(df)
        if atr_value is None or atr_value / price < fcfg.min_atr_pct:
            return None

        stop, target = short_stop_target(price, atr_value, fcfg)
        di_spread = values["minus_di"] - values["plus_di"]
        strength = min(1.0, 0.40 + 0.30 * min(values["adx"] / 40.0, 1.0)
                       + 0.30 * min(di_spread / 20.0, 1.0))

        return ShortSignal(
            strategy_id=STRATEGY_ID,
            symbol=symbol,
            signal_strength=round(strength, 4),
            trigger_price=round(price, 4),
            stop_price=stop,
            target_price=target,
            metadata={k: round(v, 2) for k, v in values.items()},
        )
    except Exception:
        log.exception("short_adx_filter.error", symbol=symbol)
        return None
