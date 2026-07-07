"""
Strategy 11 — Buying Climax / Distribution Day Reversal.

After an extended advance, a climactic day (very high volume, wide range,
new high) marks the crowd's exhaustion; the short triggers on a subsequent
reversal bar closing below the climax bar's midpoint.  The climax high is
the structural stop.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
import structlog

from short_strategies.common.config import get_short_config
from short_strategies.common.indicators import atr, pct_return
from short_strategies.common.signal import ShortSignal
from short_strategies.risk.sizing import short_stop_target

log = structlog.get_logger(__name__)

STRATEGY_ID = "short_buying_climax"


def detect(
    symbol: str,
    df: pd.DataFrame,
    config=None,
    filters=None,
    ctx=None,
) -> Optional[ShortSignal]:
    """Detect a post-climax reversal.  Never raises."""
    try:
        cfg = config or get_short_config().buying_climax
        fcfg = filters or get_short_config().filters
        min_rows = cfg.advance_lookback + cfg.climax_within_bars + 25
        if df is None or len(df) < min_rows:
            return None

        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        volume = df["Volume"].astype(float)
        price = float(close.iloc[-1])
        if price <= 0:
            return None

        # Look for the climax bar within the last climax_within_bars bars
        # (excluding today, which must be the reversal bar).
        for back in range(1, cfg.climax_within_bars + 1):
            i = len(df) - 1 - back

            # Prior advance measured up to the climax bar.
            advance = pct_return(close.iloc[: i + 1], cfg.advance_lookback)
            if advance is None or advance < cfg.advance_min_pct:
                continue

            # Climactic volume vs the 20 bars before the climax.
            avg_vol = float(volume.iloc[max(0, i - 20): i].mean())
            if avg_vol <= 0 or float(volume.iloc[i]) / avg_vol < cfg.climax_volume_ratio:
                continue

            # Climax bar makes a new high of the advance window.
            climax_high = float(high.iloc[i])
            window_high = float(high.iloc[max(0, i - cfg.advance_lookback): i].max())
            if climax_high < window_high:
                continue

            # Reversal: today closes below the climax bar's midpoint.
            climax_mid = (climax_high + float(low.iloc[i])) / 2.0
            if price >= climax_mid:
                continue

            atr_value = atr(df)
            if atr_value is None or atr_value / price < fcfg.min_atr_pct:
                return None

            structural_stop = climax_high + 0.25 * atr_value
            stop, target = short_stop_target(price, atr_value, fcfg, structural_stop)

            vol_ratio = float(volume.iloc[i]) / avg_vol
            reversal_depth = (climax_mid - price) / atr_value
            strength = min(1.0, 0.40
                           + 0.25 * min(vol_ratio / (2 * cfg.climax_volume_ratio), 1.0)
                           + 0.20 * min(advance / (2 * cfg.advance_min_pct), 1.0)
                           + 0.15 * min(reversal_depth, 1.0))

            sig = ShortSignal(
                strategy_id=STRATEGY_ID,
                symbol=symbol,
                signal_strength=round(strength, 4),
                trigger_price=round(price, 4),
                stop_price=stop,
                target_price=target,
                metadata={
                    "climax_bars_ago": back,
                    "climax_high": round(climax_high, 4),
                    "climax_volume_ratio": round(vol_ratio, 4),
                    "prior_advance": round(advance, 4),
                },
            )
            log.info("short_buying_climax.detected", symbol=symbol,
                     climax_bars_ago=back, vol_ratio=round(vol_ratio, 2))
            return sig
        return None
    except Exception:
        log.exception("short_buying_climax.error", symbol=symbol)
        return None
