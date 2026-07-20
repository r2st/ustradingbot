"""
Universe seeder — populates the stock-universe SQLite database.

Fetches US equities from SEC EDGAR (free, no auth), adds curated Canadian
stocks and major ETFs, optionally enriches with yfinance sector/price data,
and migrates the existing 41-symbol watchlist into user_watchlists.

Run standalone::

    python -m data_store.universe_seeder
    python -m data_store.universe_seeder --skip-enrichment   # faster, no yfinance
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from typing import Any

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# SIC division code -> GICS sector mapping
# ---------------------------------------------------------------------------
# Keys are 2-digit SIC division prefixes; values are approximate GICS sectors.
# Python ``range`` objects are used as keys for readability — the helper
# function ``sic_to_gics`` iterates them to find a match.

_SIC_RANGES: list[tuple[range, str]] = [
    # Agriculture, Forestry, Fishing (01-09)
    (range(1, 10), "Materials"),
    # Mining (10-14) — Energy (10-12), Materials (13-14)
    (range(10, 13), "Energy"),
    (range(13, 15), "Materials"),
    # Construction (15-17)
    (range(15, 18), "Industrials"),
    # Manufacturing (20-39) — varies by sub-industry
    (range(20, 22), "Consumer Staples"),       # Food
    (range(22, 24), "Consumer Discretionary"),  # Textiles, Apparel
    (range(24, 26), "Materials"),               # Lumber, Paper
    (range(26, 28), "Materials"),               # Paper, Printing
    (range(28, 30), "Health Care"),             # Chemicals, Pharma
    (range(30, 32), "Materials"),               # Rubber, Plastics
    (range(32, 34), "Materials"),               # Stone, Primary Metals
    (range(34, 36), "Industrials"),             # Fabricated Metals, Machinery
    (range(36, 38), "Information Technology"),   # Electronics, Instruments
    (range(38, 40), "Industrials"),             # Misc Manufacturing
    # Transportation & Utilities (40-49)
    (range(40, 48), "Industrials"),             # Transport (40-47)
    (range(48, 50), "Communication Services"),  # Communications
    # Wholesale Trade (50-51)
    (range(50, 52), "Consumer Discretionary"),
    # Retail Trade (52-59)
    (range(52, 54), "Consumer Discretionary"),
    (range(54, 55), "Consumer Staples"),        # Food stores
    (range(55, 60), "Consumer Discretionary"),
    # Finance (60-67)
    (range(60, 65), "Financials"),
    (range(65, 66), "Real Estate"),
    (range(67, 68), "Financials"),
    # Services (70-89)
    (range(70, 73), "Consumer Discretionary"),  # Hotels, Services
    (range(73, 74), "Information Technology"),   # Computer Services
    (range(74, 76), "Industrials"),             # Management, Engineering
    (range(76, 80), "Consumer Discretionary"),  # Misc Services
    (range(80, 83), "Health Care"),             # Health, Legal, Education
    (range(83, 90), "Industrials"),             # Social, Engineering Services
    # Public Administration (91-99)
    (range(91, 100), "Utilities"),
]


def sic_to_gics(sic_code: int) -> str:
    """Map a numeric SIC code to an approximate GICS sector name.

    Uses the first two digits (SIC division) to look up the sector.
    Returns ``"Unknown"`` when no mapping matches.
    """
    division = sic_code // 100 if sic_code >= 100 else sic_code
    for rng, sector in _SIC_RANGES:
        if division in rng:
            return sector
    return "Unknown"


# ---------------------------------------------------------------------------
# Curated Canadian stocks
# ---------------------------------------------------------------------------

CA_TICKERS: list[tuple[str, str]] = [
    # From existing CA_WATCHLIST in config/universe.py
    ("ATD.TO", "Alimentation Couche-Tard"),
    ("BAM.TO", "Brookfield Asset Management"),
    ("BN.TO", "Brookfield Corporation"),
    ("CNR.TO", "Canadian National Railway"),
    ("CSU.TO", "Constellation Software"),
    ("ENB.TO", "Enbridge"),
    ("MFC.TO", "Manulife Financial"),
    ("RY.TO", "Royal Bank of Canada"),
    ("SHOP.TO", "Shopify"),
    ("SU.TO", "Suncor Energy"),
    ("TD.TO", "Toronto-Dominion Bank"),
    # Additional major TSX names
    ("CP.TO", "Canadian Pacific Kansas City"),
    ("BMO.TO", "Bank of Montreal"),
    ("BCE.TO", "BCE Inc"),
    ("TRP.TO", "TC Energy"),
    ("CNQ.TO", "Canadian Natural Resources"),
    ("NTR.TO", "Nutrien"),
    ("ABX.TO", "Barrick Gold"),
    ("FTS.TO", "Fortis"),
    ("GIB-A.TO", "CGI Group"),
    ("WCN.TO", "Waste Connections"),
    ("IFC.TO", "Intact Financial"),
    ("MG.TO", "Magna International"),
    ("DOL.TO", "Dollarama"),
    ("L.TO", "Loblaw Companies"),
    ("GWO.TO", "Great-West Lifeco"),
    ("POW.TO", "Power Corporation of Canada"),
    ("FFH.TO", "Fairfax Financial Holdings"),
    ("CCL-B.TO", "CCL Industries"),
    ("WFG.TO", "West Fraser Timber"),
    ("QSR.TO", "Restaurant Brands International"),
]


# ---------------------------------------------------------------------------
# Major ETFs
# ---------------------------------------------------------------------------

MAJOR_ETFS: list[tuple[str, str, str]] = [
    # Index
    ("SPY", "SPDR S&P 500 ETF", "NYSE"),
    ("QQQ", "Invesco QQQ Trust", "NASDAQ"),
    ("IWM", "iShares Russell 2000 ETF", "NYSE"),
    ("DIA", "SPDR Dow Jones Industrial Average ETF", "NYSE"),
    # Sector
    ("XLK", "Technology Select Sector SPDR", "NYSE"),
    ("XLF", "Financial Select Sector SPDR", "NYSE"),
    ("XLE", "Energy Select Sector SPDR", "NYSE"),
    ("XLV", "Health Care Select Sector SPDR", "NYSE"),
    ("XLI", "Industrial Select Sector SPDR", "NYSE"),
    ("XLP", "Consumer Staples Select Sector SPDR", "NYSE"),
    ("XLY", "Consumer Discretionary Select Sector SPDR", "NYSE"),
    ("XLB", "Materials Select Sector SPDR", "NYSE"),
    ("XLU", "Utilities Select Sector SPDR", "NYSE"),
    ("XLRE", "Real Estate Select Sector SPDR", "NYSE"),
    ("XLC", "Communication Services Select Sector SPDR", "NYSE"),
    ("XBI", "SPDR S&P Biotech ETF", "NYSE"),
    # Fixed Income
    ("TLT", "iShares 20+ Year Treasury Bond ETF", "NASDAQ"),
    ("BND", "Vanguard Total Bond Market ETF", "NASDAQ"),
    ("HYG", "iShares iBoxx $ High Yield Corporate Bond ETF", "NYSE"),
    ("LQD", "iShares iBoxx $ Investment Grade Corporate Bond ETF", "NYSE"),
    # Commodity
    ("GLD", "SPDR Gold Shares", "NYSE"),
    ("SLV", "iShares Silver Trust", "NYSE"),
    ("USO", "United States Oil Fund", "NYSE"),
    # International
    ("EFA", "iShares MSCI EAFE ETF", "NYSE"),
    ("VWO", "Vanguard FTSE Emerging Markets ETF", "NYSE"),
    ("IEMG", "iShares Core MSCI Emerging Markets ETF", "NYSE"),
    # Thematic
    ("ARKK", "ARK Innovation ETF", "NYSE"),
    ("HACK", "ETFMG Prime Cyber Security ETF", "NYSE"),
    ("TAN", "Invesco Solar ETF", "NYSE"),
    ("SOXX", "iShares Semiconductor ETF", "NASDAQ"),
    ("SMH", "VanEck Semiconductor ETF", "NASDAQ"),
    # Broad Market
    ("VTI", "Vanguard Total Stock Market ETF", "NYSE"),
    ("VOO", "Vanguard S&P 500 ETF", "NYSE"),
    ("IVV", "iShares Core S&P 500 ETF", "NYSE"),
    # Canadian ETFs
    ("XIU.TO", "iShares S&P/TSX 60 Index ETF", "TSX"),
    ("XIC.TO", "iShares Core S&P/TSX Capped Composite Index ETF", "TSX"),
    ("ZSP.TO", "BMO S&P 500 Index ETF", "TSX"),
]


# ---------------------------------------------------------------------------
# Regex for filtering out non-common-stock tickers from SEC data
# ---------------------------------------------------------------------------

_JUNK_SUFFIX_RE = re.compile(
    r"[.\-](WS|WT|PR[A-Z]?|UN|RT|R|U|W|P[A-Z]?)$",
    re.IGNORECASE,
)


def _is_common_stock(ticker: str) -> bool:
    """Return True if *ticker* looks like a common-stock symbol.

    Filters out warrants (.WS, .WT, W suffix), preferred shares (.PR, -P),
    units (.U, .UN), and rights (.R, .RT).
    """
    if not ticker or len(ticker) > 10:
        return False
    # Contains digits mid-ticker (e.g. "XYZ1") — usually a test/class issue
    if re.search(r"\d", ticker.rstrip("0123456789")):
        return False
    if _JUNK_SUFFIX_RE.search(ticker):
        return False
    return True


# ═══════════════════════════════════════════════════════════════════════════
# Seeder
# ═══════════════════════════════════════════════════════════════════════════


class UniverseSeeder:
    """Populate the universe SQLite database from public sources.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file (created if absent).
    """

    SEC_EDGAR_URL = "https://www.sec.gov/files/company_tickers.json"

    # SEC EDGAR requires a descriptive User-Agent (they block generic ones).
    _UA = "USTradingBot/1.0 (universe-seeder)"

    def __init__(self, db_path: str | Path) -> None:
        from data_store.universe import UniverseDB

        self.db = UniverseDB(db_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def seed_all(self, skip_enrichment: bool = False) -> dict[str, Any]:
        """Run the full seeding pipeline.

        Returns a summary dict with counts of symbols added, enriched, etc.
        """
        stats: dict[str, Any] = {}

        # 1. SEC EDGAR US tickers
        sec_symbols = self.fetch_sec_tickers()
        stats["sec_fetched"] = len(sec_symbols)

        if sec_symbols:
            added = self.db.add_symbols(sec_symbols)
            stats["us_added"] = added
            log.info("sec_tickers_loaded", fetched=len(sec_symbols), added=added)
        else:
            stats["us_added"] = 0
            log.warning("sec_fetch_failed_or_empty")

        # 2. Canadian stocks
        stats["ca_added"] = self.seed_canadian()

        # 3. ETFs
        stats["etf_added"] = self.seed_etfs()

        # 3b. Index membership (S&P 500 + NASDAQ-100) — the basis for the
        #     Scan Pool (Tier 2) and Universe (Tier 3) tiers.
        stats["index_members"] = self.seed_index_membership(
            refresh_from_network=not skip_enrichment
        )

        # 4. Optional yfinance enrichment
        if not skip_enrichment:
            try:
                enriched = self._enrich_all()
                stats["enriched"] = enriched
            except Exception:
                log.exception("enrichment_failed")
                stats["enriched"] = 0
        else:
            stats["enriched"] = 0
            log.info("enrichment_skipped")

        # 5. Default scan filters
        self.seed_default_filters()
        stats["filters_set"] = True

        # 6. Migrate existing watchlist
        try:
            self.migrate_existing_watchlist()
            stats["watchlist_migrated"] = True
        except Exception:
            log.exception("watchlist_migration_failed")
            stats["watchlist_migrated"] = False

        stats.update(self.db.get_stats())
        log.info("seeding_complete", **stats)
        return stats

    # ------------------------------------------------------------------
    # SEC EDGAR
    # ------------------------------------------------------------------

    def fetch_sec_tickers(self) -> list[dict[str, Any]]:
        """Fetch US tickers from SEC EDGAR company_tickers.json.

        Returns a list of dicts ready for ``UniverseDB.add_symbols()``, each
        containing at minimum ``symbol``, ``company_name``, ``exchange``,
        ``country``, ``currency``, and ``sector``.
        """
        import httpx

        try:
            resp = httpx.get(
                self.SEC_EDGAR_URL,
                headers={"User-Agent": self._UA, "Accept-Encoding": "gzip"},
                timeout=30.0,
                follow_redirects=True,
            )
            resp.raise_for_status()
            raw: dict[str, dict] = resp.json()
        except Exception:
            log.exception("sec_edgar_fetch_error")
            return []

        symbols: list[dict[str, Any]] = []
        seen: set[str] = set()

        for _key, entry in raw.items():
            ticker: str = entry.get("ticker", "").strip().upper()
            title: str = entry.get("title", "").strip()

            if not ticker or not _is_common_stock(ticker):
                continue
            if ticker in seen:
                continue
            seen.add(ticker)

            # Derive approximate sector from CIK's SIC code if available
            sic = entry.get("sic", 0)
            sector = sic_to_gics(int(sic)) if sic else "Unknown"

            symbols.append(
                {
                    "ticker": ticker,
                    "name": title.title() if title else "",
                    "exchange": "",  # SEC data doesn't include exchange
                    "country": "US",
                    "currency": "USD",
                    "sector": sector,
                    "asset_type": "stock",
                }
            )

        log.info("sec_tickers_parsed", total=len(raw), valid=len(symbols))
        return symbols

    # ------------------------------------------------------------------
    # Canadian stocks
    # ------------------------------------------------------------------

    def seed_canadian(self) -> int:
        """Add curated Canadian TSX stocks to the universe.

        Returns the number of symbols actually added (excludes duplicates).
        """
        symbols = [
            {
                "ticker": ticker,
                "name": name,
                "exchange": "TSX",
                "country": "CA",
                "currency": "CAD",
                "sector": "",
                "asset_type": "stock",
            }
            for ticker, name in CA_TICKERS
        ]
        added = self.db.add_symbols(symbols)
        log.info("canadian_stocks_loaded", total=len(symbols), added=added)
        return added

    # ------------------------------------------------------------------
    # ETFs
    # ------------------------------------------------------------------

    def seed_etfs(self) -> int:
        """Add major ETFs to the universe.

        Returns the number of symbols actually added.
        """
        symbols = [
            {
                "ticker": ticker,
                "name": name,
                "exchange": exchange,
                "country": "CA" if ticker.endswith(".TO") else "US",
                "currency": "CAD" if ticker.endswith(".TO") else "USD",
                "sector": "",
                "asset_type": "etf",
            }
            for ticker, name, exchange in MAJOR_ETFS
        ]
        added = self.db.add_symbols(symbols)
        log.info("etfs_loaded", total=len(symbols), added=added)
        return added

    # ------------------------------------------------------------------
    # Index membership (S&P 500 + NASDAQ-100)
    # ------------------------------------------------------------------

    def seed_index_membership(self, refresh_from_network: bool = False) -> int:
        """Record S&P 500 and NASDAQ-100 membership in the universe DB.

        Constituents come from :mod:`config.index_membership`: the curated
        static lists by default, or a best-effort Wikipedia refresh when
        *refresh_from_network* is set (falls back to static on any failure).
        Each constituent is ensured to exist in the ``symbols`` table (without
        clobbering existing enrichment) so the Scan Pool can rank it.

        Returns:
            Total membership rows written across both indices.
        """
        from config.index_membership import (
            NASDAQ100_INDEX,
            SP500_INDEX,
            fetch_index_constituents,
            static_symbols,
        )

        total = 0
        for index_name in (SP500_INDEX, NASDAQ100_INDEX):
            if refresh_from_network:
                tickers = fetch_index_constituents(index_name)
            else:
                tickers = static_symbols(index_name)
            if not tickers:
                continue
            # Ensure a symbols row exists for each constituent (US common
            # stock; enrichment fills sector/price/market-cap later).
            self.db.ensure_symbols(
                [
                    {
                        "ticker": t,
                        "name": "",
                        "exchange": "",
                        "country": "US",
                        "currency": "USD",
                        "sector": "",
                        "asset_type": "stock",
                    }
                    for t in tickers
                ]
            )
            written = self.db.set_index_membership(index_name, tickers)
            total += written
            log.info(
                "index_membership_seeded",
                index=index_name,
                count=len(tickers),
                written=written,
            )
        return total

    # ------------------------------------------------------------------
    # yfinance enrichment
    # ------------------------------------------------------------------

    def enrich_batch(
        self,
        tickers: list[str],
        batch_size: int = 50,
        delay: float = 1.0,
    ) -> int:
        """Enrich symbols with yfinance data (sector, industry, price, volume).

        Processes *tickers* in batches of *batch_size*, sleeping *delay*
        seconds between batches to avoid rate limiting.  Returns the count
        of symbols successfully enriched.

        This is best-effort: individual ticker failures are logged and
        skipped so the rest of the batch proceeds.
        """
        import yfinance as yf  # noqa: F811 — lazy import

        enriched = 0

        for i in range(0, len(tickers), batch_size):
            batch = tickers[i : i + batch_size]
            log.info(
                "enriching_batch",
                batch_start=i,
                batch_size=len(batch),
                total=len(tickers),
            )

            # --- Price / volume via bulk download ---
            try:
                df = yf.download(
                    batch,
                    period="5d",
                    progress=False,
                    threads=True,
                )
                if df is not None and not df.empty:
                    self._apply_price_data(batch, df)
            except Exception:
                log.exception("yf_download_failed", batch_start=i)

            # --- Sector / industry / asset-type via individual Ticker.info ---
            for symbol in batch:
                try:
                    info = yf.Ticker(symbol).info or {}
                    updates: dict[str, Any] = {"ticker": symbol}
                    if info.get("sector"):
                        updates["sector"] = info["sector"]
                    if info.get("industry"):
                        updates["industry"] = info["industry"]
                    if info.get("marketCap"):
                        updates["market_cap"] = float(info["marketCap"])
                    if info.get("exchange"):
                        updates["exchange"] = info["exchange"]
                    # Populate asset_type from the live quoteType so ETFs the
                    # static list doesn't know about (VTI, ARKK, SCHD, …) are
                    # tagged correctly for sizing / ATR thresholds.
                    quote_type = info.get("quoteType")
                    if quote_type:
                        updates["asset_type"] = (
                            "etf" if str(quote_type).upper() == "ETF" else "stock"
                        )
                    if len(updates) > 1:  # more than just ticker
                        self.db.bulk_update([updates])
                        enriched += 1
                except Exception:
                    log.debug("yf_info_failed", symbol=symbol)

            if i + batch_size < len(tickers):
                time.sleep(delay)

        log.info("enrichment_done", enriched=enriched, total=len(tickers))
        return enriched

    def _apply_price_data(self, tickers: list[str], df: Any) -> None:
        """Extract latest close / volume from a yfinance bulk download frame."""
        import pandas as pd

        updates: list[dict[str, Any]] = []
        if isinstance(df.columns, pd.MultiIndex):
            # Multi-ticker download: columns are (metric, ticker)
            for symbol in tickers:
                try:
                    close_col = ("Close", symbol)
                    vol_col = ("Volume", symbol)
                    if close_col not in df.columns:
                        continue
                    last_close = df[close_col].dropna().iloc[-1]
                    last_vol = (
                        df[vol_col].dropna().iloc[-1]
                        if vol_col in df.columns
                        else 0
                    )
                    updates.append({
                        "ticker": symbol,
                        "last_price": float(last_close),
                        "avg_volume": float(last_vol),
                    })
                except (IndexError, KeyError):
                    pass
        else:
            # Single-ticker download
            if len(tickers) == 1 and not df.empty:
                symbol = tickers[0]
                try:
                    last_close = df["Close"].dropna().iloc[-1]
                    last_vol = (
                        df["Volume"].dropna().iloc[-1]
                        if "Volume" in df.columns
                        else 0
                    )
                    updates.append({
                        "ticker": symbol,
                        "last_price": float(last_close),
                        "avg_volume": float(last_vol),
                    })
                except (IndexError, KeyError):
                    pass
        if updates:
            self.db.bulk_update(updates)

    def _enrich_all(self) -> int:
        """Enrich every symbol currently in the database."""
        all_symbols = self.db.get_symbols(is_active=False)
        if not all_symbols:
            return 0
        all_tickers = [s["ticker"] for s in all_symbols]
        return self.enrich_batch(all_tickers)

    # ------------------------------------------------------------------
    # Default scan filters
    # ------------------------------------------------------------------

    def seed_default_filters(self) -> None:
        """Set default scan filters for the universe screener."""
        self.db.set_scan_filter("min_price", 5.0)
        self.db.set_scan_filter("min_volume", 100_000)
        self.db.set_scan_filter("min_market_cap", 300_000_000)
        log.info("default_filters_set")

    # ------------------------------------------------------------------
    # Migrate existing watchlist
    # ------------------------------------------------------------------

    def migrate_existing_watchlist(self) -> None:
        """Import the current 41-symbol universe into user_watchlists.

        Creates an "Active Watchlist" with all symbols and per-sector
        watchlists (e.g. "Technology", "Financials") derived from
        ``config.universe.SECTOR_BY_SYMBOL``.
        """
        from config.universe import ALL_SYMBOLS, SECTOR_BY_SYMBOL

        # Ensure every legacy symbol exists in the symbols table first
        legacy_symbols = [
            {
                "ticker": sym,
                "name": "",
                "exchange": "TSX" if sym.endswith(".TO") else "",
                "country": "CA" if sym.endswith(".TO") else "US",
                "currency": "CAD" if sym.endswith(".TO") else "USD",
                "sector": SECTOR_BY_SYMBOL.get(sym, ""),
                "asset_type": "stock",
            }
            for sym in ALL_SYMBOLS
        ]
        self.db.add_symbols(legacy_symbols)

        # Create the main "Active Watchlist"
        self.db.add_to_watchlist("Active Watchlist", ALL_SYMBOLS)
        log.info("active_watchlist_created", count=len(ALL_SYMBOLS))

        # Create per-sector watchlists
        sector_groups: dict[str, list[str]] = {}
        for sym, sector in SECTOR_BY_SYMBOL.items():
            sector_groups.setdefault(sector, []).append(sym)

        for sector, syms in sorted(sector_groups.items()):
            self.db.add_to_watchlist(sector, syms)
            log.info("sector_watchlist_created", sector=sector, count=len(syms))


# ═══════════════════════════════════════════════════════════════════════════
# CLI entry point
# ═══════════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Populate the stock universe database",
    )
    parser.add_argument(
        "--db-path",
        default="data_store/universe.db",
        help="Path to the SQLite database file (default: data_store/universe.db)",
    )
    parser.add_argument(
        "--skip-enrichment",
        action="store_true",
        help="Skip yfinance enrichment (faster, no network calls for price/sector data)",
    )
    args = parser.parse_args()

    seeder = UniverseSeeder(args.db_path)
    stats = seeder.seed_all(skip_enrichment=args.skip_enrichment)
    print(f"Seeding complete: {stats}")


if __name__ == "__main__":
    main()
