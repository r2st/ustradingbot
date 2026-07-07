"""
Strategy 5 — Sector/Peer Relative Strength Ranking.

Rank every scanned symbol by trailing return within its sector (falling back
to the whole scan universe when the sector is too small) and short the bottom
decile — provided the symbol is also below its own trend MA.  Requires the
scanner-supplied :class:`~short_strategies.common.context.MarketContext`.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
import structlog

from short_strategies.common.config import get_short_config
from short_strategies.common.indicators import atr, sma
from short_strategies.common.signal import ShortSignal
from short_strategies.risk.sizing import short_stop_target

log = structlog.get_logger(__name__)

STRATEGY_ID = "short_relative_weakness"


def detect(
    symbol: str,
    df: pd.DataFrame,
    config=None,
    filters=None,
    ctx=None,
) -> Optional[ShortSignal]:
    """Detect bottom-decile relative weakness.  Never raises.

    Returns ``None`` without a context (the ranking is cross-sectional).
    """
    try:
        cfg = config or get_short_config().relative_weakness
        fcfg = filters or get_short_config().filters
        if ctx is None or df is None or len(df) < cfg.ma_window + 1:
            return None
        my_ret = ctx.returns.get(symbol)
        if my_ret is None:
            return None

        peers = ctx.peers_of(symbol, cfg.min_peers)
        if len(peers) < cfg.min_peers:
            return None
        ranked = sorted(peers, key=lambda s: ctx.returns[s])
        cutoff = max(1, int(len(ranked) * cfg.bottom_decile))
        if symbol not in ranked[:cutoff]:
            return None

        close = df["Close"].astype(float)
        price = float(close.iloc[-1])
        if price <= 0:
            return None
        if cfg.require_below_ma:
            ma = sma(close, cfg.ma_window)
            if pd.isna(ma.iloc[-1]) or price >= float(ma.iloc[-1]):
                return None
        atr_value = atr(df)
        if atr_value is None or atr_value / price < fcfg.min_atr_pct:
            return None

        stop, target = short_stop_target(price, atr_value, fcfg)

        # Strength: how deep in the ranking + how negative vs the median peer.
        rank_pos = ranked.index(symbol) / max(len(ranked) - 1, 1)
        median_ret = ctx.returns[ranked[len(ranked) // 2]]
        spread = median_ret - my_ret
        strength = min(1.0, 0.40 + 0.30 * (1.0 - rank_pos / max(cfg.bottom_decile, 0.01))
                       + 0.30 * min(spread / 0.10, 1.0))
        strength = max(0.0, strength)

        sig = ShortSignal(
            strategy_id=STRATEGY_ID,
            symbol=symbol,
            signal_strength=round(strength, 4),
            trigger_price=round(price, 4),
            stop_price=stop,
            target_price=target,
            metadata={
                "trailing_return": round(my_ret, 4),
                "median_peer_return": round(median_ret, 4),
                "rank": ranked.index(symbol) + 1,
                "peers": len(ranked),
                "sector": ctx.sector_by_symbol.get(symbol, "Unknown"),
            },
        )
        log.info("short_relative_weakness.detected", symbol=symbol,
                 rank=ranked.index(symbol) + 1, peers=len(ranked))
        return sig
    except Exception:
        log.exception("short_relative_weakness.error", symbol=symbol)
        return None
