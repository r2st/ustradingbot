"""
Pre-market scanner (feature 14).

Flags watchlist symbols that are gapping off their prior close or trading on
unusual volume, before the regular session opens.  Works off daily OHLCV bars:
the last bar is treated as the most recent (pre-market / latest) print and the
bar before it as the prior close.

:func:`scan_symbol` is a pure function over an OHLCV frame; :func:`scan` wires
it to the data fetcher across a list of symbols.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

import pandas as pd

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass
class PremarketHit:
    """A symbol flagged by the pre-market scan."""

    symbol: str
    prev_close: float
    last_price: float
    gap_pct: float
    volume_ratio: float
    avg_volume: float
    last_volume: float
    signals: List[str] = field(default_factory=list)
    # Feature 4: session tag and whether the price came from a *true*
    # extended-hours quote (vs the daily-bar proxy).
    session: str = "regular"
    extended: bool = False
    unusual: bool = False

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "prev_close": round(self.prev_close, 4),
            "last_price": round(self.last_price, 4),
            "gap_pct": round(self.gap_pct, 4),
            "volume_ratio": round(self.volume_ratio, 2),
            "avg_volume": round(self.avg_volume, 0),
            "last_volume": round(self.last_volume, 0),
            "signals": list(self.signals),
            "session": self.session,
            "extended": self.extended,
            "unusual": self.unusual,
        }


def scan_symbol(symbol: str, df: pd.DataFrame, settings) -> Optional[PremarketHit]:
    """Return a :class:`PremarketHit` for *symbol* or ``None`` if nothing fired."""
    if df is None or "Close" not in getattr(df, "columns", []) or len(df) < 2:
        return None
    gap_thresh = float(getattr(settings, "PREMARKET_GAP_PCT", 0.02))
    vol_ratio_thresh = float(getattr(settings, "PREMARKET_VOLUME_RATIO", 1.5))

    close = df["Close"].astype(float)
    volume = df["Volume"].astype(float) if "Volume" in df.columns else None
    prev_close = float(close.iloc[-2])
    last_price = float(close.iloc[-1])
    if prev_close <= 0:
        return None
    gap_pct = last_price / prev_close - 1.0

    last_volume = float(volume.iloc[-1]) if volume is not None else 0.0
    avg_volume = float(volume.iloc[:-1].tail(20).mean()) if volume is not None else 0.0
    volume_ratio = (last_volume / avg_volume) if avg_volume > 0 else 0.0

    signals: List[str] = []
    if gap_pct >= gap_thresh:
        signals.append("gap_up")
    elif gap_pct <= -gap_thresh:
        signals.append("gap_down")
    if volume_ratio >= vol_ratio_thresh:
        signals.append("high_volume")

    if not signals:
        return None
    return PremarketHit(
        symbol=str(symbol).upper(),
        prev_close=prev_close,
        last_price=last_price,
        gap_pct=gap_pct,
        volume_ratio=volume_ratio,
        avg_volume=avg_volume,
        last_volume=last_volume,
        signals=signals,
    )


def scan(
    symbols: Sequence[str],
    settings,
    fetcher: Optional[Callable[[str], Optional[pd.DataFrame]]] = None,
) -> List[PremarketHit]:
    """Scan *symbols* for gaps / unusual volume.  Skips per-symbol errors."""
    if fetcher is None:
        try:
            from data.fetcher import fetch_ohlcv as fetcher  # type: ignore
        except Exception:  # noqa: BLE001
            return []
    # Feature 4: prefer true extended-hours quotes when enabled.  Any lookup
    # failure silently falls back to the daily-bar proxy below (fail-open).
    ext_quotes: dict = {}
    if getattr(settings, "EXTENDED_HOURS_ENABLED", False):
        try:
            from data.extended_hours import scan_extended_hours

            ext_quotes = {q.symbol: q for q in scan_extended_hours(symbols, settings)}
        except Exception as exc:  # noqa: BLE001
            log.warning("premarket.ext_hours_error", error=str(exc))
            ext_quotes = {}

    hits: List[PremarketHit] = []
    for symbol in symbols:
        try:
            df = fetcher(symbol)
            hit = scan_symbol(symbol, df, settings) if df is not None else None
        except Exception as exc:  # noqa: BLE001
            log.warning("premarket.scan_error", symbol=symbol, error=str(exc))
            continue
        if hit is not None:
            _apply_ext_quote(hit, ext_quotes.get(str(symbol).upper()), settings)
            hits.append(hit)
    hits.sort(key=lambda h: (abs(h.gap_pct), h.volume_ratio), reverse=True)
    return hits


def _apply_ext_quote(hit: "PremarketHit", quote, settings) -> None:
    """Overlay a true extended-hours quote onto a daily-bar proxy hit."""
    if quote is None:
        return
    hit.extended = True
    hit.session = getattr(quote, "session", hit.session) or hit.session
    hit.unusual = bool(getattr(quote, "unusual", False))
    gap_pct = getattr(quote, "gap_pct", None)
    if gap_pct is not None:
        hit.last_price = float(getattr(quote, "last", hit.last_price))
        hit.gap_pct = float(gap_pct)
        gap_thresh = float(getattr(settings, "PREMARKET_GAP_PCT", 0.02))
        signals = [s for s in hit.signals if s not in ("gap_up", "gap_down")]
        if hit.gap_pct >= gap_thresh:
            signals.append("gap_up")
        elif hit.gap_pct <= -gap_thresh:
            signals.append("gap_down")
        hit.signals = signals
