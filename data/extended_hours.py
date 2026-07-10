"""
Extended-hours (pre/post-market) quotes and overnight-gap detection (Feature 4).

The legacy pre-market scanner (``signals/premarket.py``) computes gaps from
*daily bars* — a proxy that only works once a regular-session bar exists and
can't see genuine pre-open / post-close prints.  This module asks the active
market-data provider for a *true* extended-hours quote (via the optional,
duck-typed ``get_extended_hours_quote`` method — IBKR in live mode, else Alpaca
IEX) and derives the overnight gap from it.

Everything is **fail-open**: no provider support, no data, or any error yields
``None``/empty, so a missing extended-hours feed never blocks the engine.  A
short TTL cache (``EXT_HOURS_CACHE_TTL_SECONDS``, ~60 s) keeps repeated lookups
cheap since these prices move.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import RLock
from typing import Any, Dict, List, Optional, Sequence

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_cache: Dict[str, tuple[float, Optional["ExtQuote"]]] = {}
_cache_lock = RLock()


@dataclass
class ExtQuote:
    """An extended-hours quote for one symbol."""

    symbol: str
    session: str        # "pre" | "post" | "regular" | "closed"
    last: float
    prev_close: Optional[float]
    gap_pct: Optional[float]
    ext_volume: Optional[float]
    avg_ext_volume: Optional[float]
    unusual: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "session": self.session,
            "last": round(self.last, 4),
            "prev_close": round(self.prev_close, 4) if self.prev_close is not None else None,
            "gap_pct": round(self.gap_pct, 4) if self.gap_pct is not None else None,
            "ext_volume": self.ext_volume,
            "avg_ext_volume": self.avg_ext_volume,
            "unusual": self.unusual,
        }


def _build_quote(symbol: str, raw: dict, settings: Any) -> Optional[ExtQuote]:
    """Turn a provider's raw extended-hours dict into an :class:`ExtQuote`."""
    last = raw.get("last")
    if last is None:
        return None
    try:
        last = float(last)
    except (TypeError, ValueError):
        return None
    if last <= 0:
        return None

    prev_close = raw.get("prev_close")
    prev_close = float(prev_close) if prev_close not in (None, 0) else None
    gap_pct = (last / prev_close - 1.0) if prev_close else None

    ext_volume = raw.get("ext_volume")
    avg_ext_volume = raw.get("avg_ext_volume")
    unusual_ratio = float(getattr(settings, "EXT_UNUSUAL_VOLUME_RATIO", 3.0))
    unusual = bool(
        ext_volume is not None
        and avg_ext_volume
        and float(avg_ext_volume) > 0
        and float(ext_volume) / float(avg_ext_volume) >= unusual_ratio
    )

    return ExtQuote(
        symbol=str(symbol).upper(),
        session=str(raw.get("session", "closed") or "closed"),
        last=last,
        prev_close=prev_close,
        gap_pct=gap_pct,
        ext_volume=ext_volume,
        avg_ext_volume=avg_ext_volume,
        unusual=unusual,
    )


def _provider_for(settings: Any):
    """Return a provider exposing ``get_extended_hours_quote``, or ``None``."""
    try:
        from data.providers import make_provider

        provider = make_provider(settings)
    except Exception:  # noqa: BLE001
        return None
    return provider if callable(getattr(provider, "get_extended_hours_quote", None)) else None


def overnight_gap(
    symbol: str,
    settings: Any = None,
    provider: Any = None,
) -> Optional[ExtQuote]:
    """Return the current extended-hours quote + overnight gap for *symbol*.

    Fail-open: returns ``None`` when extended hours are disabled, the provider
    lacks support, or anything errors.  TTL-cached per symbol.
    """
    symbol = str(symbol or "").upper()
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()

    if not getattr(settings, "EXTENDED_HOURS_ENABLED", False):
        return None

    ttl = float(getattr(settings, "EXT_HOURS_CACHE_TTL_SECONDS", 60.0))
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(symbol)
        if entry is not None and now - entry[0] <= ttl:
            return entry[1]

    prov = provider or _provider_for(settings)
    result: Optional[ExtQuote] = None
    if prov is not None:
        try:
            raw = prov.get_extended_hours_quote(symbol)
            if isinstance(raw, dict):
                result = _build_quote(symbol, raw, settings)
        except Exception as exc:  # noqa: BLE001 -- fail-open
            log.warning("ext_hours.lookup_failed", symbol=symbol, error=str(exc))
            result = None

    with _cache_lock:
        _cache[symbol] = (now, result)
    return result


def scan_extended_hours(
    symbols: Sequence[str],
    settings: Any = None,
    provider: Any = None,
) -> List[ExtQuote]:
    """Return extended-hours quotes for *symbols*, largest gap first.

    Skips symbols with no extended-hours data.  Never raises.
    """
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    prov = provider or _provider_for(settings)
    out: List[ExtQuote] = []
    for sym in symbols:
        q = overnight_gap(sym, settings, provider=prov)
        if q is not None:
            out.append(q)
    out.sort(key=lambda q: abs(q.gap_pct or 0.0), reverse=True)
    return out


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
