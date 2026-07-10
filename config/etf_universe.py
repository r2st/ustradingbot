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

from typing import Dict, List, Optional

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

# Fast membership set (upper-cased) for is_etf().
_ETF_SET = frozenset(ALL_ETFS)


def is_etf(symbol: str) -> bool:
    """Return ``True`` when *symbol* is one of the known ETFs.

    Case-insensitive; unknown or empty symbols return ``False``.
    """
    return str(symbol or "").upper() in _ETF_SET


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
