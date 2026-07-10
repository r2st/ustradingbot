"""
Market breadth from sector-ETF participation (Feature 3).

A simple, robust breadth indicator: of the 11 SPDR sector ETFs, how many are
trading above their own 50-day moving average?  Broad participation (most
sectors above their MA) confirms a healthy tape; narrow participation (only a
couple holding up) warns that a rally is thin.

Pure over an injectable OHLCV fetcher, so it tests without the network and is
**fail-open** — a missing symbol is skipped, and total failure yields a neutral
"unknown" reading rather than raising.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass
class BreadthResult:
    """Sector-participation breadth snapshot."""

    above: int                # sector ETFs above their 50-day MA
    total: int                # sector ETFs successfully measured
    pct: float                # above / total, 0..1 (0.0 when total == 0)
    label: str                # "strong" | "neutral" | "weak" | "unknown"
    per_sector: Dict[str, bool]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "above": self.above,
            "total": self.total,
            "pct": round(self.pct, 4),
            "label": self.label,
            "per_sector": dict(self.per_sector),
        }


def _sma(closes: List[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _label(pct: float, total: int) -> str:
    if total == 0:
        return "unknown"
    if pct >= 0.66:
        return "strong"
    if pct <= 0.33:
        return "weak"
    return "neutral"


def sector_breadth(
    settings: Any = None,
    fetcher: Optional[Callable[[str], Any]] = None,
    ma_period: int = 50,
) -> BreadthResult:
    """Return the sector-participation breadth reading.

    Args:
        settings: Application settings (unused today; kept for symmetry / future
            thresholds).
        fetcher: ``fetcher(symbol) -> DataFrame`` with a ``Close`` column;
            defaults to :func:`data.fetcher.fetch_ohlcv`.
        ma_period: Moving-average window (default 50 days).
    """
    from config.etf_universe import SECTOR_ETFS

    if fetcher is None:
        try:
            from data.fetcher import fetch_ohlcv as fetcher  # type: ignore
        except Exception:  # noqa: BLE001
            return BreadthResult(0, 0, 0.0, "unknown", {})

    per_sector: Dict[str, bool] = {}
    above = 0
    total = 0
    for sector, etf in SECTOR_ETFS.items():
        try:
            df = fetcher(etf)
            if df is None or "Close" not in getattr(df, "columns", []) or len(df) < ma_period:
                continue
            closes = [float(x) for x in df["Close"].tolist()]
            ma = _sma(closes, ma_period)
            if ma is None:
                continue
            is_above = closes[-1] > ma
            per_sector[sector] = is_above
            total += 1
            if is_above:
                above += 1
        except Exception as exc:  # noqa: BLE001 -- one bad ETF must not break breadth
            log.warning("breadth.sector_failed", sector=sector, etf=etf, error=str(exc))
            continue

    pct = (above / total) if total else 0.0
    return BreadthResult(above, total, pct, _label(pct, total), per_sector)
