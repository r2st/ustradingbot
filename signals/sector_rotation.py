"""
Sector-rotation strategy (Feature 3).

Ranks the 11 SPDR sector ETFs by relative strength — a blend of trailing return
and trend versus the broad market (SPY) — and emits long signals on the top-N
rotating *leaders*: sectors that are both in an uptrend (above their 50-day MA)
and outperforming SPY over the lookback.  This is a classic momentum-of-sectors
rotation: ride whichever sectors institutional money is rotating into.

New strategy id ``"sector_rotation"``.  Signals flow through the *same*
scan→gate→execute pipeline as every other strategy; ETF sizing (Feature 3) and
the natural earnings-filter no-op for ETFs both apply automatically.

Pure over injectable OHLCV fetchers, TTL-agnostic, and **fail-open**: any error
yields no signal rather than raising.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Optional

import numpy as np
import pandas as pd
import structlog

from config.settings import get_settings
from signals.signal_types import Grade, Signal

log = structlog.get_logger(__name__)

_MIN_ROWS = 80  # need > lookback (63) + MA(50) headroom


@dataclass
class SectorRank:
    """Relative-strength ranking for one sector ETF."""

    etf: str
    sector: str
    ret_lookback: float      # trailing return over the lookback window
    rel_strength: float      # ret_lookback - SPY's return over the same window
    above_ma50: bool
    score: float             # 0..1 composite used for grading

    def to_dict(self) -> dict:
        return {
            "etf": self.etf,
            "sector": self.sector,
            "ret_lookback": round(self.ret_lookback, 4),
            "rel_strength": round(self.rel_strength, 4),
            "above_ma50": self.above_ma50,
            "score": round(self.score, 4),
        }


def _lookback_return(closes: pd.Series, lookback: int) -> Optional[float]:
    if len(closes) <= lookback:
        return None
    past = float(closes.iloc[-1 - lookback])
    if past <= 0:
        return None
    return float(closes.iloc[-1]) / past - 1.0


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    val = tr.ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    return float(val) if not pd.isna(val) else 0.0


def rank_sectors(
    settings: Any = None,
    fetcher: Optional[Callable[[str], Optional[pd.DataFrame]]] = None,
    benchmark_df: Optional[pd.DataFrame] = None,
) -> List[SectorRank]:
    """Rank the sector ETFs by relative strength, strongest first.

    Fail-open: sectors with insufficient data are skipped; a benchmark failure
    degrades ``rel_strength`` to the raw return (benchmark return treated as 0).
    """
    settings = settings or get_settings()
    if fetcher is None:
        from data.fetcher import fetch_ohlcv as fetcher  # type: ignore

    from config.etf_universe import SECTOR_ETFS

    lookback = int(getattr(settings, "SECTOR_ROTATION_LOOKBACK_DAYS", 63))
    benchmark = str(getattr(settings, "REGIME_BENCHMARK", "SPY") or "SPY")

    # Benchmark return over the same window (0.0 if unavailable).
    bench_ret = 0.0
    try:
        bdf = benchmark_df if benchmark_df is not None else fetcher(benchmark)
        if bdf is not None and "Close" in getattr(bdf, "columns", []):
            r = _lookback_return(bdf["Close"].astype(float), lookback)
            bench_ret = r if r is not None else 0.0
    except Exception:  # noqa: BLE001
        bench_ret = 0.0

    ranks: List[SectorRank] = []
    for sector, etf in SECTOR_ETFS.items():
        try:
            df = fetcher(etf)
            if df is None or "Close" not in getattr(df, "columns", []) or len(df) < _MIN_ROWS:
                continue
            closes = df["Close"].astype(float)
            ret = _lookback_return(closes, lookback)
            if ret is None:
                continue
            ma50 = float(closes.rolling(50).mean().iloc[-1])
            above = float(closes.iloc[-1]) > ma50
            rel = ret - bench_ret
            # Composite score: relative strength dominates; being in an uptrend
            # is a bonus.  Squashed into [0, 1] around a 0.5 midpoint.
            score = float(np.clip(0.5 + rel * 2.5 + (0.1 if above else -0.1), 0.0, 1.0))
            ranks.append(SectorRank(etf, sector, ret, rel, above, score))
        except Exception as exc:  # noqa: BLE001 -- one bad ETF must not break ranking
            log.warning("sector_rotation.rank_failed", etf=etf, error=str(exc))
            continue

    ranks.sort(key=lambda r: r.score, reverse=True)
    return ranks


def _build_signal(rank: SectorRank, df: pd.DataFrame, settings: Any) -> Optional[Signal]:
    """Build a long :class:`Signal` for a rotating-leader sector ETF."""
    close = df["Close"].astype(float)
    price = float(close.iloc[-1])
    if price <= 0:
        return None
    atr = _atr(df)
    if atr <= 0:
        return None
    stop_mult = float(getattr(settings, "ATR_STOP_MULTIPLIER", 1.5))
    rr = float(getattr(settings, "RISK_REWARD_MIN", 1.8))
    stop_price = price - stop_mult * atr
    risk = price - stop_price
    if risk <= 0:
        return None
    target_price = price + risk * rr
    return Signal(
        symbol=rank.etf,
        strategy="sector_rotation",
        direction="long",
        entry_price=round(price, 4),
        stop_price=round(stop_price, 4),
        target_price=round(target_price, 4),
        signal_strength=round(rank.score, 4),
        grade=Grade.from_score(rank.score),
        raw_data={
            "sector": rank.sector,
            "rel_strength": round(rank.rel_strength, 4),
            "ret_lookback": round(rank.ret_lookback, 4),
            "above_ma50": rank.above_ma50,
        },
    )


def run_sector_rotation_scan(
    settings: Any = None,
    fetcher: Optional[Callable[[str], Optional[pd.DataFrame]]] = None,
    min_grade: str = "B",
    top_n: Optional[int] = None,
) -> List[Signal]:
    """Emit long signals on the top-N rotating sector leaders.

    A leader must be outperforming the benchmark (``rel_strength > 0``) and in
    an uptrend (``above_ma50``).  Returns at most *top_n* signals meeting
    *min_grade*, strongest first.  Never raises.
    """
    settings = settings or get_settings()
    if fetcher is None:
        from data.fetcher import fetch_ohlcv as fetcher  # type: ignore

    if top_n is None:
        top_n = int(getattr(settings, "SECTOR_ROTATION_TOP_N", 3))

    _GRADE_RANK = {"A": 0, "B": 1, "C": 2, "F": 3}
    min_rank = _GRADE_RANK.get(str(min_grade).upper(), 1)

    ranks = rank_sectors(settings, fetcher)
    leaders = [r for r in ranks if r.rel_strength > 0 and r.above_ma50]

    signals: List[Signal] = []
    for rank in leaders:
        if len(signals) >= top_n:
            break
        try:
            df = fetcher(rank.etf)
            if df is None or len(df) < _MIN_ROWS:
                continue
            sig = _build_signal(rank, df, settings)
            if sig is None:
                continue
            if _GRADE_RANK.get(sig.grade.value, 3) > min_rank:
                continue
            signals.append(sig)
        except Exception as exc:  # noqa: BLE001
            log.warning("sector_rotation.signal_failed", etf=rank.etf, error=str(exc))
            continue

    if signals:
        log.info("sector_rotation.scan", leaders=len(leaders), signals=len(signals))
    return signals
