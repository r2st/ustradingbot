"""
ETF universe — broad-market and sector ETFs as first-class watchlist members.

Adds an *asset-type* concept the rest of the codebase previously lacked: every
symbol was implicitly an individual stock.  ETFs behave differently from single
names — they carry no single-company earnings date (so the earnings filter is a
natural no-op), their realised volatility is lower (so the ATR floor and the
notional cap are relaxed), and the 11 SPDR sector ETFs drive market-breadth and
the sector-rotation strategy.

The GICS-sector → ETF mapping is a static table keyed by the *same* sector
strings :data:`config.universe.SECTOR_BY_SYMBOL` uses, so
:func:`config.universe.get_sector` keeps working for ETFs and the risk
dashboard's concentration math is unchanged.

Everything here is pure/static — no network, no settings — so it is cheap to
import from hot paths (sizing, the ATR veto) and trivially testable.
"""

from __future__ import annotations

import time
from threading import RLock
from typing import Callable, Dict, List, Optional, Tuple

# ── Broad-market ETFs ───────────────────────────────────────────────────────
BROAD_MARKET_ETFS: List[str] = ["SPY", "QQQ", "IWM", "DIA"]

# ── Sector ETFs (State Street SPDR select-sector funds) ─────────────────────
# Keyed by the GICS sector strings used across config.universe so that
# get_sector(), the risk dashboard, and the watchlist seeder all agree.
SECTOR_ETFS: Dict[str, str] = {
    "Technology": "XLK",
    "Financials": "XLF",
    "Energy": "XLE",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Consumer Staples": "XLP",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
    "Materials": "XLB",
    "Consumer Discretionary": "XLY",
}

# Reverse map: ETF → GICS sector.
_SECTOR_BY_ETF: Dict[str, str] = {etf: sector for sector, etf in SECTOR_ETFS.items()}

# Every ETF the bot knows about (sorted, de-duplicated).
ALL_ETFS: List[str] = sorted(set(BROAD_MARKET_ETFS) | set(SECTOR_ETFS.values()))

# Fast membership set (upper-cased) for static ETF recognition.  Includes both
# the plain-ETF universe (ALL_ETFS) and the curated leveraged/inverse tickers —
# geared products are ETFs too, and must be recognised so the risk manager sizes
# them with their (shrunk) leverage parameters instead of stock parameters.
from config.etf_classification import KNOWN_GEARED_SYMBOLS as _KNOWN_GEARED

_ETF_SET = frozenset(ALL_ETFS) | _KNOWN_GEARED


# ── Dynamic recognition cache ───────────────────────────────────────────────
# ``is_etf`` resolves an unknown symbol against the universe DB and, as a last
# resort, a live yfinance ``quoteType`` lookup.  Those results are TTL-cached
# per symbol so the sizing / ATR-veto hot paths stay cheap and yfinance is never
# hammered.  Statically-known ETFs short-circuit before any of that.
_resolve_cache: Dict[str, Tuple[float, bool]] = {}
_resolve_lock = RLock()

# Default resolution TTL (seconds) when settings can't be read.
_DEFAULT_RESOLVE_TTL = 720.0 * 60.0


def is_etf_static(symbol: str) -> bool:
    """Return ``True`` when *symbol* is in the curated static ETF list.

    The offline, network-free membership test — the fail-open backbone the
    dynamic :func:`is_etf` falls back to.
    """
    return str(symbol or "").upper() in _ETF_SET


def clear_etf_cache() -> None:
    """Clear the dynamic ETF-resolution cache (used by tests / after seeding)."""
    with _resolve_lock:
        _resolve_cache.clear()


def _resolve_ttl_seconds() -> float:
    """Resolution-cache TTL in seconds (from settings; fails open to 12 h)."""
    try:
        from config.settings import get_settings

        return float(get_settings().ETF_DETECT_TTL_MINUTES) * 60.0
    except Exception:  # noqa: BLE001
        return _DEFAULT_RESOLVE_TTL


def _db_asset_type(sym: str) -> Optional[str]:
    """Return the universe-DB ``asset_type`` for *sym* (``"etf"``/``"stock"``/None)."""
    try:
        from config.settings import get_settings
        from data_store.universe import db_exists, get_universe_db

        settings = get_settings()
        if not db_exists(settings.DATA_DIR):
            return None
        return get_universe_db(settings.DATA_DIR).get_asset_type(sym)
    except Exception:  # noqa: BLE001 — never break sizing on a DB hiccup
        return None


def _yfinance_is_etf(sym: str) -> Optional[bool]:
    """Live yfinance ``quoteType`` check (``True``/``False``/``None``), fail-open."""
    try:
        from data.etf_metadata import is_etf_via_yfinance

        return is_etf_via_yfinance(sym)
    except Exception:  # noqa: BLE001
        return None


def _resolve_is_etf(sym: str) -> bool:
    """Resolve ETF-ness for *sym* across the three sources, in spec order.

    (1) universe DB ``asset_type`` column → (2) static list → (3) yfinance
    ``quoteType``.  Any failure falls through to the next source; a total miss
    returns ``False``.
    """
    # (1) Universe DB asset_type (the enriched source of truth).
    db_type = _db_asset_type(sym)
    if db_type is not None:
        return db_type == "etf"

    # (2) Static curated list (offline fallback).
    if sym in _ETF_SET:
        return True

    # (3) Live yfinance quoteType (last resort; may be disabled).
    yf_result = _yfinance_is_etf(sym)
    if yf_result is not None:
        return yf_result

    # Nothing recognised it → not an ETF.
    return False


def is_etf(symbol: str, clock: Callable[[], float] = time.monotonic) -> bool:
    """Return ``True`` when *symbol* is an ETF (dynamic, TTL-cached).

    Resolution order (each layer falls open to the next):

    1. The universe DB's ``asset_type`` column — covers every seeded/enriched
       symbol, including user-added funds (VTI, ARKK, SCHD, …).
    2. The curated static list in this module (offline safety net).
    3. A live ``yfinance`` ``quoteType`` lookup as a last resort.

    Results are cached per symbol for ``ETF_DETECT_TTL_MINUTES`` so repeated
    calls from the sizing / veto hot paths cost a dict lookup.  Statically-known
    ETFs always resolve ``True`` even with no DB or network.  Case-insensitive;
    empty symbols return ``False``.

    Args:
        symbol: Ticker string.
        clock: Injectable monotonic clock (test seam for cache expiry).
    """
    sym = str(symbol or "").upper()
    if not sym:
        return False

    # Static members are unconditionally ETFs — skip the cache and any I/O.
    if sym in _ETF_SET:
        return True

    ttl = _resolve_ttl_seconds()
    now = clock()
    with _resolve_lock:
        entry = _resolve_cache.get(sym)
        if entry is not None and now - entry[0] <= ttl:
            return entry[1]

    result = _resolve_is_etf(sym)

    with _resolve_lock:
        _resolve_cache[sym] = (now, result)
    return result


def asset_type(symbol: str) -> str:
    """Classify *symbol* as ``"etf"`` or ``"stock"``.

    The single source of truth for the asset-type distinction the sizing,
    ATR-veto, and gap-threshold code branch on.
    """
    return "etf" if is_etf(symbol) else "stock"


def sector_for_etf(symbol: str) -> Optional[str]:
    """Return the GICS sector a sector ETF tracks, or ``None``.

    Broad-market ETFs (SPY/QQQ/…) and non-ETFs return ``None`` — they map to no
    single sector.
    """
    return _SECTOR_BY_ETF.get(str(symbol or "").upper())


def etf_for_sector(sector: str) -> Optional[str]:
    """Return the sector ETF that tracks *sector*, or ``None`` if unmapped."""
    return SECTOR_ETFS.get(str(sector or ""))


def sector_etf_symbols() -> List[str]:
    """Return the 11 sector-ETF tickers (sorted)."""
    return sorted(SECTOR_ETFS.values())
