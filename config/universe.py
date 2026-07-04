"""
Trading universe — curated watchlists of US and Canadian equities.

This module defines the set of symbols the scanner evaluates on each cycle.
Symbols can be added or removed here without touching any other module.
"""

from __future__ import annotations

# ── US equities (NYSE / NASDAQ) ─────────────────────────────────────────────

US_WATCHLIST: list[str] = [
    "AAPL",
    "ABBV",
    "ADBE",
    "AMD",
    "AMZN",
    "ANET",
    "AVGO",
    "COIN",
    "COST",
    "CRM",
    "GOOGL",
    "HD",
    "JPM",
    "KO",
    "LLY",
    "MA",
    "META",
    "MRK",
    "MSFT",
    "NFLX",
    "NOW",
    "NVDA",
    "ORCL",
    "PANW",
    "PEP",
    "PLTR",
    "TSLA",
    "UBER",
    "UNH",
    "V",
]

# ── Canadian equities (TSX) ─────────────────────────────────────────────────

CA_WATCHLIST: list[str] = [
    "ATD.TO",
    "BAM.TO",
    "BN.TO",
    "CNR.TO",
    "CSU.TO",
    "ENB.TO",
    "MFC.TO",
    "RY.TO",
    "SHOP.TO",
    "SU.TO",
    "TD.TO",
]

# ── Combined universe (sorted, deduplicated) ────────────────────────────────

ALL_SYMBOLS: list[str] = sorted(set(US_WATCHLIST + CA_WATCHLIST))


# ── Sector / industry classification ────────────────────────────────────────
# Coarse GICS-style sector tags for every symbol in the universe, used by the
# risk dashboard to measure sector concentration in the open book.  Symbols not
# listed here resolve to ``"Unknown"``.

SECTOR_BY_SYMBOL: dict[str, str] = {
    # US — Technology
    "AAPL": "Technology",
    "ADBE": "Technology",
    "AMD": "Technology",
    "ANET": "Technology",
    "AVGO": "Technology",
    "CRM": "Technology",
    "MSFT": "Technology",
    "NOW": "Technology",
    "NVDA": "Technology",
    "ORCL": "Technology",
    "PANW": "Technology",
    "PLTR": "Technology",
    "V": "Financials",
    "MA": "Financials",
    "JPM": "Financials",
    # US — Communication Services
    "GOOGL": "Communication Services",
    "META": "Communication Services",
    "NFLX": "Communication Services",
    # US — Consumer
    "AMZN": "Consumer Discretionary",
    "HD": "Consumer Discretionary",
    "TSLA": "Consumer Discretionary",
    "UBER": "Consumer Discretionary",
    "COST": "Consumer Staples",
    "KO": "Consumer Staples",
    "PEP": "Consumer Staples",
    # US — Health Care
    "ABBV": "Health Care",
    "LLY": "Health Care",
    "MRK": "Health Care",
    "UNH": "Health Care",
    # US — Crypto-exposed
    "COIN": "Financials",
    # Canada — TSX
    "ATD.TO": "Consumer Staples",
    "BAM.TO": "Financials",
    "BN.TO": "Financials",
    "CNR.TO": "Industrials",
    "CSU.TO": "Technology",
    "ENB.TO": "Energy",
    "MFC.TO": "Financials",
    "RY.TO": "Financials",
    "SHOP.TO": "Technology",
    "SU.TO": "Energy",
    "TD.TO": "Financials",
}


def get_sector(symbol: str) -> str:
    """Return the sector tag for *symbol* (``"Unknown"`` if unclassified)."""
    return SECTOR_BY_SYMBOL.get(symbol.upper(), "Unknown")


def is_canadian(symbol: str) -> bool:
    """Return ``True`` if *symbol* trades on a Canadian exchange.

    The heuristic is simple: any ticker ending with ``.TO`` (Toronto Stock
    Exchange) is treated as Canadian.

    Args:
        symbol: Ticker string, e.g. ``"SHOP.TO"`` or ``"AAPL"``.
    """
    return symbol.upper().endswith(".TO")


def get_currency(symbol: str) -> str:
    """Return the settlement currency for *symbol*.

    Args:
        symbol: Ticker string.

    Returns:
        ``"CAD"`` for TSX-listed tickers (suffix ``.TO``), ``"USD"`` otherwise.
    """
    return "CAD" if is_canadian(symbol) else "USD"
