"""
Index membership — S&P 500 and NASDAQ-100 constituent lists.

This module is the single data source that *narrows* the tradable universe to
the two indices the operator approved: the S&P 500 and the NASDAQ-100.  It
replaces the ~10,000-symbol SEC EDGAR dump as the basis for tiered scanning.

Two access paths, in priority order:

1. **Curated static lists** (:data:`SP500` and :data:`NASDAQ_100`) — embedded
   here so the bot works fully offline and every tier is deterministic in
   tests.  These are large/mid-cap constituents and are *periodically updated
   by hand* (index reconstitution happens quarterly).  ``BRK-B`` / ``BF-B`` use
   the hyphenated class-share form that the yfinance/Alpaca providers expect.
2. **Network refresh** (:func:`fetch_index_constituents`) — a best-effort pull
   of the authoritative constituent list from Wikipedia, used by the universe
   seeder to expand the static core to the full index.  Always falls back to
   the static list on any failure, so a network outage never empties a tier.

Tier mapping (see :mod:`data_store.universe` and the engine tier loop):

* **Tier 1 (Active Trading)** — the user watchlist ∪ ETFs ∪ promoted symbols.
* **Tier 2 (Scan Pool)** — the top-N S&P 500 names by volume × market cap.
* **Tier 3 (Universe)** — the full S&P 500 ∪ NASDAQ-100 (this module).
"""

from __future__ import annotations

from typing import List

import structlog

log = structlog.get_logger(__name__)

SP500_INDEX = "SP500"
NASDAQ100_INDEX = "NASDAQ100"

# Wikipedia constituent tables (free, no auth). Used only by the seeder's
# best-effort refresh; the static lists below are the offline source of truth.
_WIKI_URLS = {
    SP500_INDEX: "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    NASDAQ100_INDEX: "https://en.wikipedia.org/wiki/Nasdaq-100",
}


# ---------------------------------------------------------------------------
# NASDAQ-100 — curated static constituents
# ---------------------------------------------------------------------------

NASDAQ_100: List[str] = sorted(set([
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "AMAT", "AMD", "AMGN",
    "AMZN", "ANSS", "APP", "ARM", "ASML", "AVGO", "AXON", "AZN", "BKNG", "BKR",
    "CCEP", "CDNS", "CDW", "CEG", "CHTR", "CMCSA", "COST", "CPRT", "CRWD", "CSCO",
    "CSGP", "CSX", "CTAS", "CTSH", "DASH", "DDOG", "DXCM", "EA", "EXC", "FANG",
    "FAST", "FTNT", "GEHC", "GFS", "GILD", "GOOG", "GOOGL", "HON", "IDXX", "INTC",
    "INTU", "ISRG", "KDP", "KHC", "KLAC", "LIN", "LRCX", "LULU", "MAR", "MCHP",
    "MDLZ", "MELI", "META", "MNST", "MRVL", "MSFT", "MU", "NFLX", "NVDA", "NXPI",
    "ODFL", "ON", "ORLY", "PANW", "PAYX", "PCAR", "PDD", "PEP", "PLTR", "PYPL",
    "QCOM", "REGN", "ROP", "ROST", "SBUX", "SNPS", "TEAM", "TMUS", "TSLA", "TTD",
    "TTWO", "TXN", "VRSK", "VRTX", "WBD", "WDAY", "XEL", "ZS",
]))


# ---------------------------------------------------------------------------
# S&P 500 — curated static constituents (large/mid-cap core across all sectors)
# ---------------------------------------------------------------------------
# Not necessarily every one of the 500 at any instant (membership churns), but
# a comprehensive large/mid-cap set that covers the top-by-market-cap names the
# Scan Pool (Tier 2) ranks over.  The seeder's Wikipedia refresh fills the tail.

SP500: List[str] = sorted(set([
    # Technology
    "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "ADBE", "AMD", "ACN", "CSCO",
    "TXN", "QCOM", "INTU", "IBM", "NOW", "AMAT", "ADI", "MU", "LRCX", "KLAC",
    "PANW", "SNPS", "CDNS", "ANET", "ROP", "MSI", "FTNT", "NXPI", "MCHP", "ON",
    "GLW", "HPQ", "HPE", "DELL", "WDC", "STX", "KEYS", "MPWR", "TDY", "TER",
    "TYL", "PTC", "ANSS", "CDW", "ZBRA", "JBL", "SWKS", "NTAP", "AKAM", "JNPR",
    "GEN", "FSLR", "ENPH", "SMCI", "TRMB", "EPAM",
    # Communication Services
    "GOOGL", "GOOG", "META", "NFLX", "DIS", "CMCSA", "T", "VZ", "TMUS", "CHTR",
    "WBD", "EA", "TTWO", "OMC", "IPG", "LYV", "MTCH", "NWSA", "NWS", "FOXA",
    "FOX", "PARA",
    # Consumer Discretionary
    "AMZN", "TSLA", "HD", "MCD", "LOW", "NKE", "SBUX", "BKNG", "TJX", "ORLY",
    "MAR", "GM", "F", "HLT", "AZO", "ROST", "YUM", "CMG", "DHI", "LEN", "NVR",
    "PHM", "ULTA", "EBAY", "APTV", "LULU", "TSCO", "DRI", "GRMN", "EXPE", "POOL",
    "DPZ", "BBY", "KMX", "RL", "TPR", "WHR", "HAS", "MGM", "WYNN", "LVS", "CCL",
    "RCL", "NCLH", "ABNB", "DASH", "UBER",
    # Consumer Staples
    "PG", "KO", "PEP", "COST", "WMT", "PM", "MO", "MDLZ", "CL", "TGT", "KMB",
    "GIS", "SYY", "KHC", "STZ", "KDP", "HSY", "MNST", "KR", "ADM", "MKC", "CHD",
    "CLX", "K", "HRL", "SJM", "CAG", "CPB", "TAP", "TSN", "DG", "DLTR", "KVUE",
    "BG", "LW", "BF-B",
    # Health Care
    "LLY", "UNH", "JNJ", "MRK", "ABBV", "TMO", "ABT", "DHR", "PFE", "AMGN",
    "ISRG", "BMY", "MDT", "GILD", "VRTX", "CVS", "CI", "ELV", "REGN", "ZTS",
    "BSX", "SYK", "BDX", "HCA", "MCK", "HUM", "BIIB", "IDXX", "IQV", "MRNA",
    "DXCM", "EW", "A", "CNC", "GEHC", "RMD", "WST", "MTD", "COR", "CAH", "ZBH",
    "BAX", "STE", "HOLX", "ALGN", "DGX", "LH", "PODD", "MOH", "TECH", "INCY",
    "CRL", "RVTY", "UHS", "DVA", "SOLV",
    # Financials
    "BRK-B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "AXP", "SPGI", "BLK",
    "C", "SCHW", "CB", "PGR", "MMC", "FI", "BX", "USB", "PNC", "AON", "ICE",
    "CME", "TFC", "MCO", "AJG", "COF", "AFL", "MET", "TRV", "ALL", "BK", "AIG",
    "PRU", "MSCI", "AMP", "DFS", "FIS", "GPN", "KKR", "HIG", "WTW", "ACGL",
    "TROW", "STT", "NDAQ", "CBOE", "CINF", "MTB", "FITB", "HBAN", "RF", "CFG",
    "KEY", "PFG", "NTRS", "SYF", "L", "GL", "BRO", "RJF", "WRB", "MKTX", "JKHY",
    "IVZ", "BEN", "COIN",
    # Industrials
    "GE", "CAT", "RTX", "HON", "UNP", "BA", "DE", "LMT", "UPS", "ADP", "ETN",
    "GD", "NOC", "EMR", "CSX", "ITW", "FDX", "NSC", "WM", "PH", "TDG", "TT",
    "CTAS", "GEV", "MMM", "PCAR", "CARR", "JCI", "CPRT", "PAYX", "CMI", "OTIS",
    "FAST", "AME", "RSG", "ODFL", "VRSK", "EFX", "URI", "ROK", "DAL", "GWW",
    "IR", "PWR", "FTV", "DOV", "XYL", "HWM", "WAB", "LUV", "UAL", "LDOS", "HUBB",
    "SNA", "PNR", "JBHT", "SWK", "TXT", "MAS", "AOS", "ALLE", "NDSN", "DAY",
    "ROL", "PAYC", "EXPD", "CHRW", "BLDR", "GNRC", "VLTO", "AXON",
    # Energy
    "XOM", "CVX", "COP", "EOG", "SLB", "MPC", "PSX", "WMB", "OKE", "VLO", "HES",
    "OXY", "KMI", "HAL", "DVN", "FANG", "BKR", "TRGP", "CTRA", "EQT", "APA",
    "LNG",
    # Materials
    "LIN", "SHW", "APD", "ECL", "FCX", "NEM", "NUE", "DOW", "DD", "CTVA", "VMC",
    "MLM", "PPG", "ALB", "IFF", "LYB", "STLD", "BALL", "AVY", "IP", "PKG", "CF",
    "MOS", "FMC", "EMN", "CE", "AMCR", "SW",
    # Real Estate
    "PLD", "AMT", "EQIX", "WELL", "SPG", "PSA", "O", "DLR", "CCI", "CBRE", "EXR",
    "VICI", "AVB", "IRM", "EQR", "SBAC", "VTR", "INVH", "ESS", "MAA", "ARE",
    "KIM", "UDR", "HST", "REG", "BXP", "FRT", "DOC", "CPT",
    # Utilities
    "NEE", "SO", "DUK", "CEG", "AEP", "SRE", "D", "EXC", "XEL", "PEG", "ED",
    "PCG", "EIX", "WEC", "AEE", "DTE", "ETR", "ES", "FE", "PPL", "CMS", "CNP",
    "ATO", "AES", "LNT", "NI", "EVRG", "PNW", "NRG", "VST",
    # Large-cap tech also in NASDAQ-100 but S&P members
    "AMZN", "GOOGL", "META", "NVDA", "TSLA", "PLTR", "PYPL", "ADSK", "CTSH",
    "GILD", "MDLZ", "MNST", "KHC", "MRVL", "CSGP", "GEHC",
]))


# ---------------------------------------------------------------------------
# Tier-2 fallback ranking — priority order when the DB has no volume/mktcap yet
# ---------------------------------------------------------------------------
# Largest, most-liquid S&P 500 names first.  Used only as a deterministic
# stand-in when the universe DB lacks the metadata to rank the Scan Pool.

SP500_LIQUIDITY_PRIORITY: List[str] = [
    "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "GOOG", "AVGO", "TSLA",
    "BRK-B", "LLY", "JPM", "V", "UNH", "XOM", "MA", "COST", "HD", "PG", "JNJ",
    "ORCL", "NFLX", "ABBV", "BAC", "CVX", "KO", "AMD", "CRM", "MRK", "WMT",
    "PEP", "TMO", "LIN", "ACN", "MCD", "ADBE", "CSCO", "ABT", "GE", "QCOM",
    "DIS", "INTU", "TXN", "IBM", "CAT", "AXP", "NOW", "AMGN", "PM", "ISRG",
    "GS", "VZ", "T", "SPGI", "PLTR", "NEE", "MS", "RTX", "UBER", "PFE",
]


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------


def sp500_symbols() -> List[str]:
    """Return the curated static S&P 500 constituent list (sorted)."""
    return list(SP500)


def nasdaq100_symbols() -> List[str]:
    """Return the curated static NASDAQ-100 constituent list (sorted)."""
    return list(NASDAQ_100)


def index_universe() -> List[str]:
    """Return the sorted union of S&P 500 and NASDAQ-100 (Tier 3 universe)."""
    return sorted(set(SP500) | set(NASDAQ_100))


def in_sp500(symbol: str) -> bool:
    """Return whether *symbol* is in the curated S&P 500 list."""
    return symbol.upper() in _SP500_SET


def in_nasdaq100(symbol: str) -> bool:
    """Return whether *symbol* is in the curated NASDAQ-100 list."""
    return symbol.upper() in _NASDAQ100_SET


def indices_for(symbol: str) -> List[str]:
    """Return the index names *symbol* belongs to (``[]`` if neither)."""
    up = symbol.upper()
    out: List[str] = []
    if up in _SP500_SET:
        out.append(SP500_INDEX)
    if up in _NASDAQ100_SET:
        out.append(NASDAQ100_INDEX)
    return out


def static_symbols(index_name: str) -> List[str]:
    """Return the curated static list for *index_name* (``[]`` if unknown)."""
    if index_name == SP500_INDEX:
        return sp500_symbols()
    if index_name == NASDAQ100_INDEX:
        return nasdaq100_symbols()
    return []


_SP500_SET = frozenset(SP500)
_NASDAQ100_SET = frozenset(NASDAQ_100)


# ---------------------------------------------------------------------------
# Best-effort network refresh (used by the seeder only)
# ---------------------------------------------------------------------------


def fetch_index_constituents(index_name: str) -> List[str]:
    """Fetch authoritative constituents for *index_name* from Wikipedia.

    Best-effort: returns the parsed ticker list on success, or the curated
    static list on any failure (network error, parse error, empty result) so a
    caller never receives an empty universe.  Requires ``pandas`` +
    ``lxml``/``html5lib`` for table parsing; degrades to static if unavailable.

    Args:
        index_name: :data:`SP500_INDEX` or :data:`NASDAQ100_INDEX`.

    Returns:
        Sorted, de-duplicated ticker list (yfinance-style class-share form).
    """
    url = _WIKI_URLS.get(index_name)
    if not url:
        return static_symbols(index_name)
    try:
        import pandas as pd

        tables = pd.read_html(url)
        for table in tables:
            symbol_col = None
            for candidate in ("symbol", "ticker", "ticker symbol"):
                for c in table.columns:
                    if str(c).strip().lower() == candidate:
                        symbol_col = c
                        break
                if symbol_col is not None:
                    break
            if symbol_col is None:
                continue
            raw = [str(v).strip().upper() for v in table[symbol_col].tolist()]
            tickers = sorted({_normalize(t) for t in raw if t and t != "NAN"})
            if len(tickers) >= 50:  # sanity: a real constituent table
                log.info(
                    "index_membership.fetched",
                    index=index_name,
                    count=len(tickers),
                )
                return tickers
    except Exception:  # noqa: BLE001 — any failure falls back to static
        log.warning("index_membership.fetch_failed", index=index_name, exc_info=True)
    return static_symbols(index_name)


def _normalize(ticker: str) -> str:
    """Normalise a Wikipedia ticker to the provider (yfinance) class-share form.

    Wikipedia renders class shares with a dot (``BRK.B``); yfinance/Alpaca use
    a hyphen (``BRK-B``).  Everything else passes through unchanged.
    """
    return ticker.replace(".", "-") if "." in ticker else ticker
