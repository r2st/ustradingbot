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
