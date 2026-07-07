"""
Cross-sectional market context for the relative-weakness strategies.

Strategies 5 (sector relative strength) and 6 (laggard fade) rank a symbol
against its peers and the benchmark, which a single-symbol detector cannot
compute alone.  The scanner builds one :class:`MarketContext` per scan and
passes it to every detector; detectors that do not need it ignore ``ctx``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import pandas as pd

from short_strategies.common.indicators import pct_return


@dataclass
class MarketContext:
    """Shared cross-sectional data for one scan.

    Attributes:
        benchmark_df: Daily OHLCV frame for the benchmark (e.g. SPY).
        returns: Trailing ``rank_lookback_days`` return per scanned symbol.
        day_returns: Last-bar (close vs prior close) return per symbol.
        sector_by_symbol: Sector tag per symbol (``"Unknown"`` when untagged).
    """

    benchmark_df: Optional[pd.DataFrame] = None
    returns: Dict[str, float] = field(default_factory=dict)
    day_returns: Dict[str, float] = field(default_factory=dict)
    sector_by_symbol: Dict[str, str] = field(default_factory=dict)

    @property
    def benchmark_day_return(self) -> Optional[float]:
        """Benchmark last-bar return, or ``None`` when unavailable."""
        if self.benchmark_df is None or len(self.benchmark_df) < 2:
            return None
        close = self.benchmark_df["Close"].astype(float)
        prev = float(close.iloc[-2])
        return float(close.iloc[-1]) / prev - 1.0 if prev > 0 else None

    def peers_of(self, symbol: str, min_peers: int) -> List[str]:
        """Symbols in *symbol*'s sector, or every ranked symbol when the
        sector has fewer than *min_peers* members (or is untagged)."""
        sector = self.sector_by_symbol.get(symbol, "Unknown")
        peers = [
            s for s, sec in self.sector_by_symbol.items()
            if sec == sector and s in self.returns
        ]
        if sector == "Unknown" or len(peers) < min_peers:
            return [s for s in self.returns]
        return peers


def build_market_context(
    symbols: List[str],
    frames: Dict[str, pd.DataFrame],
    benchmark_df: Optional[pd.DataFrame],
    rank_lookback_days: int,
    sector_lookup: Callable[[str], str],
) -> MarketContext:
    """Assemble a :class:`MarketContext` from already-fetched frames."""
    ctx = MarketContext(benchmark_df=benchmark_df)
    for sym in symbols:
        df = frames.get(sym)
        if df is None or len(df) < 2:
            continue
        close = df["Close"].astype(float)
        ret = pct_return(close, rank_lookback_days)
        if ret is not None:
            ctx.returns[sym] = ret
        prev = float(close.iloc[-2])
        if prev > 0:
            ctx.day_returns[sym] = float(close.iloc[-1]) / prev - 1.0
        ctx.sector_by_symbol[sym] = sector_lookup(sym)
    return ctx
