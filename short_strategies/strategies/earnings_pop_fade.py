"""
Strategy 9 — Post-Earnings Pop Fade.

An earnings-driven gap-up that lacks follow-through: within
``days_after_earnings`` of the report, the pop day (or a subsequent bar)
closes below its open and gives back a substantial fraction of the gap.
The earnings date comes from :mod:`data.earnings_calendar` by default and is
injectable for tests/backtests via the *earnings_days_ago* argument.

Note the shared earnings-*blackout* filter (upcoming earnings) does not
conflict with this strategy: its catalyst is in the past.
"""

from __future__ import annotations

from datetime import date
from typing import Callable, Optional

import pandas as pd
import structlog

from short_strategies.common.config import get_short_config
from short_strategies.common.indicators import atr
from short_strategies.common.signal import ShortSignal
from short_strategies.risk.sizing import short_stop_target

log = structlog.get_logger(__name__)

STRATEGY_ID = "short_earnings_pop_fade"


def _default_days_since_earnings(symbol: str) -> Optional[int]:
    """Days since the most recent earnings report, via yfinance calendar.

    ``data.earnings_calendar.next_earnings_date`` only looks forward, so this
    inspects the yfinance earnings-dates frame directly.  Returns ``None``
    when unavailable (the detector then declines — fail-closed, since the
    whole thesis is earnings-driven).
    """
    try:
        import yfinance as yf  # local import: keep module import cheap

        edf = yf.Ticker(symbol).get_earnings_dates(limit=8)
        if edf is None or edf.empty:
            return None
        today = date.today()
        past = [
            d.date() for d in edf.index
            if hasattr(d, "date") and d.date() <= today
        ]
        if not past:
            return None
        return (today - max(past)).days
    except Exception:  # noqa: BLE001 -- any lookup failure means "unknown"
        return None


def detect(
    symbol: str,
    df: pd.DataFrame,
    config=None,
    filters=None,
    ctx=None,
    days_since_earnings_fn: Optional[Callable[[str], Optional[int]]] = None,
) -> Optional[ShortSignal]:
    """Detect a fading post-earnings pop.  Never raises."""
    try:
        cfg = config or get_short_config().earnings_pop_fade
        fcfg = filters or get_short_config().filters
        if df is None or len(df) < 30:
            return None

        fn = days_since_earnings_fn or _default_days_since_earnings
        days_ago = fn(symbol)
        if days_ago is None or days_ago > cfg.days_after_earnings or days_ago < 0:
            return None

        # Find the pop (gap-up) bar within the window after earnings.
        closes = df["Close"].astype(float)
        opens = df["Open"].astype(float)
        window = min(cfg.days_after_earnings + 1, len(df) - 1)
        pop_idx = None
        for back in range(window):
            i = len(df) - 1 - back
            gap = opens.iloc[i] / closes.iloc[i - 1] - 1.0
            if gap >= cfg.gap_min_pct:
                pop_idx = i
                break
        if pop_idx is None:
            return None

        pop_open = float(opens.iloc[pop_idx])
        pre_close = float(closes.iloc[pop_idx - 1])
        price = float(closes.iloc[-1])
        if price <= 0:
            return None

        # Follow-through failed: latest close below the pop day's open and
        # a substantial share of the gap given back.
        gap_size = pop_open - pre_close
        if gap_size <= 0 or price >= pop_open:
            return None
        giveback = (pop_open - price) / gap_size
        if giveback < cfg.fade_min_pct:
            return None

        atr_value = atr(df)
        if atr_value is None or atr_value / price < fcfg.min_atr_pct:
            return None

        # Structural stop above the post-earnings high.
        post_high = float(df["High"].astype(float).iloc[pop_idx:].max())
        structural_stop = post_high + 0.25 * atr_value
        stop, target = short_stop_target(price, atr_value, fcfg, structural_stop)

        gap_pct = gap_size / pre_close
        strength = min(1.0, 0.40
                       + 0.30 * min(gap_pct / (2 * cfg.gap_min_pct), 1.0)
                       + 0.30 * min(giveback, 1.0))

        sig = ShortSignal(
            strategy_id=STRATEGY_ID,
            symbol=symbol,
            signal_strength=round(strength, 4),
            trigger_price=round(price, 4),
            stop_price=stop,
            target_price=target,
            metadata={
                "days_since_earnings": days_ago,
                "gap_pct": round(gap_pct, 4),
                "giveback": round(giveback, 4),
                "pop_open": round(pop_open, 4),
                "post_earnings_high": round(post_high, 4),
            },
        )
        log.info("short_earnings_pop_fade.detected", symbol=symbol,
                 giveback=round(giveback, 3))
        return sig
    except Exception:
        log.exception("short_earnings_pop_fade.error", symbol=symbol)
        return None
