"""
SQLite-backed universe database for the stock scanner.

Manages the full symbol universe (US + Canadian equities and ETFs), user
watchlists, and scan filters.  Replaces the hard-coded lists in
:mod:`config.universe` with a queryable, filterable database that supports
tiered scanning:

* **Tier 1** — symbols on the user's active (enabled) watchlists.
* **Tier 2** — all active symbols in a given GICS sector.
* **Tier 3** — the full universe after applying scan filters (price, volume,
  market cap).

The on-disk file is ``DATA_DIR/universe.db``.  All writes are serialised
through a :class:`threading.Lock` for thread safety; reads use ``sqlite3.Row``
so every query returns a list of plain dicts.

Usage::

    from data_store.universe import get_universe_db

    db = get_universe_db("data_store")
    db.add_symbols([{"ticker": "AAPL", "name": "Apple Inc.", ...}])
    tier1 = db.get_tier1_symbols()
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from config.settings import EASTERN

_DB_FILENAME = "universe.db"

# Default scan filters seeded on first run.
_DEFAULT_FILTERS: list[tuple[str, float]] = [
    ("min_price", 5.0),
    ("min_volume", 100_000),
    ("min_market_cap", 300_000_000),
]


# ---------------------------------------------------------------------------
# Main database class
# ---------------------------------------------------------------------------


class UniverseDB:
    """Thread-safe SQLite manager for the stock universe.

    Args:
        db_path: Path to the ``universe.db`` file.  Parent directories are
            created automatically if they do not exist.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_tables()

    # ------------------------------------------------------------------ DDL

    def _ensure_tables(self) -> None:
        """Create the schema tables and seed default scan filters."""
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS symbols (
                    ticker       TEXT PRIMARY KEY,
                    name         TEXT,
                    exchange     TEXT NOT NULL,
                    asset_type   TEXT NOT NULL,
                    sector       TEXT,
                    industry     TEXT,
                    market_cap   REAL,
                    avg_volume   REAL,
                    last_price   REAL,
                    currency     TEXT,
                    country      TEXT,
                    is_active    BOOLEAN DEFAULT 1,
                    last_updated TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS user_watchlists (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    list_name TEXT NOT NULL,
                    ticker    TEXT NOT NULL REFERENCES symbols(ticker),
                    enabled   BOOLEAN DEFAULT 1,
                    added_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(list_name, ticker)
                );

                CREATE TABLE IF NOT EXISTS scan_filters (
                    filter_name  TEXT PRIMARY KEY,
                    filter_value REAL,
                    enabled      BOOLEAN DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS index_membership (
                    ticker     TEXT NOT NULL,
                    index_name TEXT NOT NULL,
                    PRIMARY KEY (ticker, index_name)
                );
                CREATE INDEX IF NOT EXISTS idx_index_membership_name
                    ON index_membership(index_name);

                CREATE TABLE IF NOT EXISTS promotions (
                    ticker      TEXT PRIMARY KEY,
                    source_tier TEXT,
                    reason      TEXT,
                    promoted_at TIMESTAMP,
                    expires_at  TIMESTAMP
                );
                """
            )
            # Seed default scan filters if the table is empty.
            row = cur.execute("SELECT COUNT(*) FROM scan_filters").fetchone()
            if row[0] == 0:
                cur.executemany(
                    "INSERT OR IGNORE INTO scan_filters (filter_name, filter_value, enabled) "
                    "VALUES (?, ?, 1)",
                    _DEFAULT_FILTERS,
                )
            self._conn.commit()

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
        """Convert a list of ``sqlite3.Row`` objects to plain dicts."""
        return [dict(r) for r in rows]

    def _now(self) -> str:
        """Return the current Eastern-time ISO timestamp."""
        return datetime.now(tz=EASTERN).isoformat(timespec="seconds")

    # -------------------------------------------------------- symbol CRUD

    def add_symbols(self, symbols: list[dict]) -> int:
        """Bulk upsert symbols into the ``symbols`` table.

        Each dict should have keys matching column names (at minimum
        ``ticker``, ``exchange``, ``asset_type``).  Missing optional columns
        are set to ``NULL``.

        Returns:
            Number of rows inserted or updated.
        """
        if not symbols:
            return 0

        columns = [
            "ticker", "name", "exchange", "asset_type", "sector", "industry",
            "market_cap", "avg_volume", "last_price", "currency", "country",
            "is_active", "last_updated",
        ]
        now = self._now()
        count = 0
        with self._lock:
            cur = self._conn.cursor()
            for sym in symbols:
                vals = [sym.get(c) for c in columns]
                # Default is_active to 1 if not supplied.
                if vals[columns.index("is_active")] is None:
                    vals[columns.index("is_active")] = 1
                # Always stamp last_updated.
                vals[columns.index("last_updated")] = now
                placeholders = ", ".join("?" for _ in columns)
                col_names = ", ".join(columns)
                cur.execute(
                    f"INSERT OR REPLACE INTO symbols ({col_names}) VALUES ({placeholders})",
                    vals,
                )
                count += cur.rowcount
            self._conn.commit()
        return count

    def ensure_symbols(self, symbols: list[dict]) -> int:
        """Insert symbols only if their ticker is not already present.

        Unlike :meth:`add_symbols` (which upserts via ``INSERT OR REPLACE`` and
        would wipe already-enriched columns), this uses ``INSERT OR IGNORE`` so
        existing rows — and any sector/price/market-cap enrichment they carry —
        are left untouched.  Used when recording index membership for tickers
        that may or may not already exist.

        Returns:
            Number of rows actually inserted.
        """
        if not symbols:
            return 0
        columns = [
            "ticker", "name", "exchange", "asset_type", "sector", "industry",
            "market_cap", "avg_volume", "last_price", "currency", "country",
            "is_active", "last_updated",
        ]
        now = self._now()
        count = 0
        with self._lock:
            cur = self._conn.cursor()
            for sym in symbols:
                vals = [sym.get(c) for c in columns]
                if vals[columns.index("is_active")] is None:
                    vals[columns.index("is_active")] = 1
                vals[columns.index("last_updated")] = now
                placeholders = ", ".join("?" for _ in columns)
                col_names = ", ".join(columns)
                cur.execute(
                    f"INSERT OR IGNORE INTO symbols ({col_names}) VALUES ({placeholders})",
                    vals,
                )
                count += cur.rowcount
            self._conn.commit()
        return count

    def get_symbols(
        self,
        exchange: str | None = None,
        sector: str | None = None,
        asset_type: str | None = None,
        country: str | None = None,
        is_active: bool = True,
        min_price: float | None = None,
        min_volume: float | None = None,
        min_market_cap: float | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict]:
        """Query symbols with optional filters.

        Args:
            exchange: Filter by exchange name (e.g. ``"NASDAQ"``).
            sector: Filter by GICS sector.
            asset_type: ``"STOCK"`` or ``"ETF"``.
            country: ``"US"`` or ``"CA"``.
            is_active: If *True* (default), only return active symbols.
            min_price: Minimum ``last_price``.
            min_volume: Minimum ``avg_volume``.
            min_market_cap: Minimum ``market_cap``.
            search: Case-insensitive LIKE match on ticker or name.
            limit: Maximum rows to return.
            offset: Row offset for pagination.

        Returns:
            List of symbol dicts.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if is_active:
            clauses.append("is_active = 1")
        if exchange is not None:
            clauses.append("exchange = ?")
            params.append(exchange)
        if sector is not None:
            clauses.append("sector = ?")
            params.append(sector)
        if asset_type is not None:
            clauses.append("asset_type = ?")
            params.append(asset_type)
        if country is not None:
            clauses.append("country = ?")
            params.append(country)
        if min_price is not None:
            clauses.append("last_price >= ?")
            params.append(min_price)
        if min_volume is not None:
            clauses.append("avg_volume >= ?")
            params.append(min_volume)
        if min_market_cap is not None:
            clauses.append("market_cap >= ?")
            params.append(min_market_cap)
        if search is not None:
            clauses.append("(ticker LIKE ? OR name LIKE ?)")
            pattern = f"%{search}%"
            params.extend([pattern, pattern])

        where = " AND ".join(clauses) if clauses else "1"
        sql = f"SELECT * FROM symbols WHERE {where} ORDER BY ticker"

        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        if offset is not None:
            sql += " OFFSET ?"
            params.append(offset)

        rows = self._conn.execute(sql, params).fetchall()
        return self._rows_to_dicts(rows)

    def get_symbols_by_sector(self, sector: str, is_active: bool = True) -> list[dict]:
        """Get all symbols in a GICS sector.

        Args:
            sector: Sector name (e.g. ``"Technology"``).
            is_active: If *True*, only return active symbols.
        """
        return self.get_symbols(sector=sector, is_active=is_active)

    def get_symbols_by_exchange(self, exchange: str, is_active: bool = True) -> list[dict]:
        """Get all symbols on an exchange.

        Args:
            exchange: Exchange name (e.g. ``"NASDAQ"``).
            is_active: If *True*, only return active symbols.
        """
        return self.get_symbols(exchange=exchange, is_active=is_active)

    def search_symbols(self, query: str, limit: int = 50) -> list[dict]:
        """Case-insensitive search by ticker or company name.

        Args:
            query: Partial ticker or name to match.
            limit: Maximum results (default 50).
        """
        pattern = f"%{query}%"
        rows = self._conn.execute(
            "SELECT * FROM symbols WHERE ticker LIKE ? COLLATE NOCASE "
            "OR name LIKE ? COLLATE NOCASE ORDER BY ticker LIMIT ?",
            (pattern, pattern, limit),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_sectors(self) -> list[dict]:
        """Return sectors with symbol counts.

        Returns:
            ``[{"sector": "Technology", "count": 500}, ...]``
        """
        rows = self._conn.execute(
            "SELECT sector, COUNT(*) AS count FROM symbols "
            "WHERE is_active = 1 AND sector IS NOT NULL "
            "GROUP BY sector ORDER BY sector"
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_exchanges(self) -> list[dict]:
        """Return exchanges with symbol counts.

        Returns:
            ``[{"exchange": "NASDAQ", "count": 3000}, ...]``
        """
        rows = self._conn.execute(
            "SELECT exchange, COUNT(*) AS count FROM symbols "
            "WHERE is_active = 1 GROUP BY exchange ORDER BY exchange"
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_stats(self) -> dict:
        """Return aggregate universe statistics.

        Returns:
            Dict with keys: ``total_symbols``, ``active_symbols``,
            ``by_exchange``, ``by_sector``, ``by_asset_type``,
            ``last_updated``.
        """
        cur = self._conn.cursor()
        total = cur.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        active = cur.execute(
            "SELECT COUNT(*) FROM symbols WHERE is_active = 1"
        ).fetchone()[0]
        last = cur.execute(
            "SELECT MAX(last_updated) FROM symbols"
        ).fetchone()[0]

        by_exchange = {
            r["exchange"]: r["count"] for r in self.get_exchanges()
        }
        by_sector = {
            r["sector"]: r["count"] for r in self.get_sectors()
        }

        asset_rows = cur.execute(
            "SELECT asset_type, COUNT(*) AS count FROM symbols "
            "WHERE is_active = 1 GROUP BY asset_type"
        ).fetchall()
        by_asset_type = {r["asset_type"]: r["count"] for r in asset_rows}

        return {
            "total_symbols": total,
            "active_symbols": active,
            "by_exchange": by_exchange,
            "by_sector": by_sector,
            "by_asset_type": by_asset_type,
            "last_updated": last,
        }

    # ---------------------------------------------------------- watchlists

    def add_to_watchlist(self, list_name: str, tickers: list[str]) -> int:
        """Add tickers to a named watchlist.

        Tickers that are already in the list are silently skipped.

        Args:
            list_name: Watchlist name (e.g. ``"Top Picks"``).
            tickers: Ticker strings to add.

        Returns:
            Number of tickers actually inserted.
        """
        if not tickers:
            return 0
        count = 0
        with self._lock:
            cur = self._conn.cursor()
            for t in tickers:
                try:
                    cur.execute(
                        "INSERT OR IGNORE INTO user_watchlists (list_name, ticker) "
                        "VALUES (?, ?)",
                        (list_name, t.upper()),
                    )
                    count += cur.rowcount
                except sqlite3.IntegrityError:
                    # FK violation — ticker not in symbols table.
                    continue
            self._conn.commit()
        return count

    def remove_from_watchlist(self, list_name: str, tickers: list[str]) -> int:
        """Remove tickers from a named watchlist.

        Args:
            list_name: Watchlist name.
            tickers: Tickers to remove.

        Returns:
            Number of rows deleted.
        """
        if not tickers:
            return 0
        count = 0
        with self._lock:
            cur = self._conn.cursor()
            for t in tickers:
                cur.execute(
                    "DELETE FROM user_watchlists WHERE list_name = ? AND ticker = ?",
                    (list_name, t.upper()),
                )
                count += cur.rowcount
            self._conn.commit()
        return count

    def get_watchlist(self, list_name: str) -> list[dict]:
        """Get all symbols in a named watchlist (joined with full symbol data).

        Args:
            list_name: Watchlist name.

        Returns:
            List of symbol dicts augmented with ``list_name``, ``enabled``,
            and ``added_at`` from the watchlist entry.
        """
        rows = self._conn.execute(
            "SELECT s.*, w.list_name, w.enabled, w.added_at "
            "FROM user_watchlists w "
            "JOIN symbols s ON s.ticker = w.ticker "
            "WHERE w.list_name = ? "
            "ORDER BY s.ticker",
            (list_name,),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_active_watchlist(self) -> list[str]:
        """Return tickers from all enabled watchlists (the Tier 1 scan list).

        Returns:
            Sorted, deduplicated list of ticker strings.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT w.ticker FROM user_watchlists w "
            "WHERE w.enabled = 1 ORDER BY w.ticker"
        ).fetchall()
        return [r["ticker"] for r in rows]

    def get_watchlist_names(self) -> list[dict]:
        """Return watchlist names with counts and enabled status.

        Returns:
            ``[{"list_name": "Top Picks", "count": 12, "enabled": 1}, ...]``
        """
        rows = self._conn.execute(
            "SELECT list_name, COUNT(*) AS count, "
            "MIN(enabled) AS enabled "
            "FROM user_watchlists GROUP BY list_name ORDER BY list_name"
        ).fetchall()
        return self._rows_to_dicts(rows)

    def set_watchlist_enabled(self, list_name: str, enabled: bool) -> None:
        """Enable or disable all entries in a named watchlist.

        Args:
            list_name: Watchlist name.
            enabled: *True* to enable, *False* to disable.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE user_watchlists SET enabled = ? WHERE list_name = ?",
                (int(enabled), list_name),
            )
            self._conn.commit()

    def delete_watchlist(self, list_name: str) -> None:
        """Delete all entries for a named watchlist.

        Args:
            list_name: Watchlist name to delete.
        """
        with self._lock:
            self._conn.execute(
                "DELETE FROM user_watchlists WHERE list_name = ?",
                (list_name,),
            )
            self._conn.commit()

    # --------------------------------------------------------- scan filters

    def get_scan_filters(self) -> dict:
        """Return active scan filters as ``{filter_name: filter_value}``.

        Only enabled filters are included.
        """
        rows = self._conn.execute(
            "SELECT filter_name, filter_value FROM scan_filters WHERE enabled = 1"
        ).fetchall()
        return {r["filter_name"]: r["filter_value"] for r in rows}

    def set_scan_filter(
        self, filter_name: str, filter_value: float, enabled: bool = True
    ) -> None:
        """Set or update a scan filter.

        Args:
            filter_name: Filter key (e.g. ``"min_price"``).
            filter_value: Numeric threshold.
            enabled: Whether the filter is active.
        """
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO scan_filters "
                "(filter_name, filter_value, enabled) VALUES (?, ?, ?)",
                (filter_name, filter_value, int(enabled)),
            )
            self._conn.commit()

    # ------------------------------------------------------ bulk operations

    def bulk_update(self, updates: list[dict]) -> None:
        """Update fields for multiple symbols.

        Each dict must contain ``"ticker"`` plus any combination of
        ``last_price``, ``avg_volume``, ``market_cap``, ``sector``,
        ``industry``, ``exchange``.

        Args:
            updates: List of update dicts.
        """
        if not updates:
            return
        updatable = (
            "last_price", "avg_volume", "market_cap",
            "sector", "industry", "exchange",
        )
        now = self._now()
        with self._lock:
            cur = self._conn.cursor()
            for u in updates:
                ticker = u.get("ticker")
                if not ticker:
                    continue
                sets: list[str] = []
                vals: list[Any] = []
                for col in updatable:
                    if col in u:
                        sets.append(f"{col} = ?")
                        vals.append(u[col])
                if not sets:
                    continue
                sets.append("last_updated = ?")
                vals.append(now)
                vals.append(ticker)
                cur.execute(
                    f"UPDATE symbols SET {', '.join(sets)} WHERE ticker = ?",
                    vals,
                )
            self._conn.commit()

    def mark_inactive(self, tickers: list[str]) -> None:
        """Mark symbols as inactive (delisted / removed from universe).

        Args:
            tickers: Ticker strings to deactivate.
        """
        if not tickers:
            return
        now = self._now()
        with self._lock:
            cur = self._conn.cursor()
            for t in tickers:
                cur.execute(
                    "UPDATE symbols SET is_active = 0, last_updated = ? WHERE ticker = ?",
                    (now, t.upper()),
                )
            self._conn.commit()

    def get_filtered_universe(self) -> list[str]:
        """Apply all active scan filters and return qualifying tickers.

        Filters are matched by name: ``min_price`` maps to
        ``last_price >= ?``, ``min_volume`` to ``avg_volume >= ?``, and
        ``min_market_cap`` to ``market_cap >= ?``.

        Returns:
            Sorted list of ticker strings that pass every active filter.
        """
        filters = self.get_scan_filters()
        clauses: list[str] = ["is_active = 1"]
        params: list[Any] = []

        column_map = {
            "min_price": "last_price",
            "min_volume": "avg_volume",
            "min_market_cap": "market_cap",
        }
        for fname, fval in filters.items():
            col = column_map.get(fname)
            if col is not None:
                clauses.append(f"{col} >= ?")
                params.append(fval)

        where = " AND ".join(clauses)
        rows = self._conn.execute(
            f"SELECT ticker FROM symbols WHERE {where} ORDER BY ticker",
            params,
        ).fetchall()
        return [r["ticker"] for r in rows]

    # --------------------------------------------------------- tier support

    def get_tier1_symbols(self) -> list[str]:
        """Tier 1 = active watchlist symbols.

        Returns:
            Sorted, deduplicated ticker list.
        """
        return self.get_active_watchlist()

    def get_tier2_symbols(self, sector: str) -> list[str]:
        """Tier 2 = all active symbols in a given sector.

        Args:
            sector: GICS sector name.

        Returns:
            Sorted ticker list.
        """
        rows = self._conn.execute(
            "SELECT ticker FROM symbols WHERE sector = ? AND is_active = 1 "
            "ORDER BY ticker",
            (sector,),
        ).fetchall()
        return [r["ticker"] for r in rows]

    def get_tier3_symbols(self) -> list[str]:
        """Tier 3 = full filtered universe.

        Returns:
            Sorted ticker list produced by :meth:`get_filtered_universe`.
        """
        return self.get_filtered_universe()

    # ---------------------------------------------------- index membership

    def set_index_membership(self, index_name: str, tickers: list[str]) -> int:
        """Replace the membership of *index_name* with *tickers*.

        Existing membership rows for the index are deleted first so the table
        always reflects the latest constituent list (index reconstitution
        removes names as well as adding them).  Membership is intentionally not
        foreign-keyed to ``symbols`` — a constituent may be recorded before its
        symbol row is enriched.

        Args:
            index_name: e.g. ``"SP500"`` or ``"NASDAQ100"``.
            tickers: Constituent ticker strings.

        Returns:
            Number of membership rows written.
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "DELETE FROM index_membership WHERE index_name = ?", (index_name,)
            )
            cur.executemany(
                "INSERT OR IGNORE INTO index_membership (ticker, index_name) "
                "VALUES (?, ?)",
                [(t.upper(), index_name) for t in tickers],
            )
            self._conn.commit()
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(tickers)

    def get_index_symbols(self, index_name: str) -> list[str]:
        """Return the sorted tickers recorded for *index_name*."""
        rows = self._conn.execute(
            "SELECT ticker FROM index_membership WHERE index_name = ? "
            "ORDER BY ticker",
            (index_name,),
        ).fetchall()
        return [r["ticker"] for r in rows]

    def get_index_universe(self) -> list[str]:
        """Return the sorted union of every recorded index's members (Tier 3)."""
        rows = self._conn.execute(
            "SELECT DISTINCT ticker FROM index_membership ORDER BY ticker"
        ).fetchall()
        return [r["ticker"] for r in rows]

    def is_in_index(self, ticker: str, index_name: str) -> bool:
        """Return whether *ticker* is recorded in *index_name*."""
        row = self._conn.execute(
            "SELECT 1 FROM index_membership WHERE ticker = ? AND index_name = ? "
            "LIMIT 1",
            (ticker.upper(), index_name),
        ).fetchone()
        return row is not None

    def get_scan_pool(self, index_name: str = "SP500", limit: int | None = None) -> list[str]:
        """Return the Tier-2 Scan Pool: top members of *index_name* by liquidity.

        Members are ranked by ``avg_volume * market_cap`` (a dollar-volume-ish
        proxy) descending, so the most liquid, largest names come first.  Rows
        missing either metric sort last (they still appear, so a freshly-seeded
        DB with no enrichment yet returns the full membership in ticker order).
        Only active symbols are considered.

        Args:
            index_name: Index to draw the pool from (default ``"SP500"``).
            limit: Maximum symbols to return (``None`` = all members).

        Returns:
            Ranked ticker list, longest at *limit*.
        """
        sql = (
            "SELECT m.ticker AS ticker, "
            "       COALESCE(s.avg_volume, 0) * COALESCE(s.market_cap, 0) AS liq, "
            "       COALESCE(s.is_active, 1) AS active "
            "FROM index_membership m "
            "LEFT JOIN symbols s ON s.ticker = m.ticker "
            "WHERE m.index_name = ? AND COALESCE(s.is_active, 1) = 1 "
            "ORDER BY liq DESC, m.ticker ASC"
        )
        params: list[Any] = [index_name]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [r["ticker"] for r in rows]

    def get_scan_pool_ranked(
        self, index_name: str = "SP500", limit: int | None = None
    ) -> list[dict]:
        """Return the Tier-2 Scan Pool with the ranking metadata visible.

        Same ranking as :meth:`get_scan_pool` (``avg_volume * market_cap``
        descending) but each row carries the raw metrics and its 1-based rank
        so the dashboard can show *why* a symbol is in the pool.

        Args:
            index_name: Index to draw the pool from (default ``"SP500"``).
            limit: Maximum symbols to return (``None`` = all members).

        Returns:
            List of ``{"rank", "ticker", "name", "sector", "market_cap",
            "avg_volume", "last_price", "liq"}`` dicts, most-liquid first.
        """
        sql = (
            "SELECT m.ticker AS ticker, s.name AS name, s.sector AS sector, "
            "       s.market_cap AS market_cap, s.avg_volume AS avg_volume, "
            "       s.last_price AS last_price, "
            "       COALESCE(s.avg_volume, 0) * COALESCE(s.market_cap, 0) AS liq "
            "FROM index_membership m "
            "LEFT JOIN symbols s ON s.ticker = m.ticker "
            "WHERE m.index_name = ? AND COALESCE(s.is_active, 1) = 1 "
            "ORDER BY liq DESC, m.ticker ASC"
        )
        params: list[Any] = [index_name]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        out = self._rows_to_dicts(rows)
        for i, row in enumerate(out, start=1):
            row["rank"] = i
        return out

    # ------------------------------------------------------------ promotions

    def promote_symbol(
        self,
        ticker: str,
        source_tier: str = "tier2",
        reason: str = "",
        ttl_hours: float = 72.0,
    ) -> None:
        """Promote *ticker* into Tier 1 for *ttl_hours* hours.

        A promoted symbol joins the Active-Trading scan set (full strategy
        evaluation every cycle) until it expires.  Re-promoting an existing
        symbol refreshes its expiry.

        Args:
            ticker: Symbol to promote.
            source_tier: Which tier surfaced the signal (``"tier2"``/``"tier3"``).
            reason: Human-readable reason (e.g. the strategy/grade that fired).
            ttl_hours: Lifetime of the promotion; non-positive means no expiry.
        """
        from datetime import timedelta

        now = datetime.now(tz=EASTERN)
        expires = (
            (now + timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
            if ttl_hours and ttl_hours > 0
            else None
        )
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO promotions "
                "(ticker, source_tier, reason, promoted_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    ticker.upper(),
                    source_tier,
                    reason,
                    now.isoformat(timespec="seconds"),
                    expires,
                ),
            )
            self._conn.commit()

    def get_promoted_symbols(self) -> list[str]:
        """Return the tickers currently promoted to Tier 1 (non-expired).

        A row with a ``NULL`` ``expires_at`` never expires.  Expiry comparison
        is lexicographic over same-timezone ISO strings, which is order-correct.
        """
        now = self._now()
        rows = self._conn.execute(
            "SELECT ticker FROM promotions "
            "WHERE expires_at IS NULL OR expires_at > ? "
            "ORDER BY ticker",
            (now,),
        ).fetchall()
        return [r["ticker"] for r in rows]

    def get_promotions(self) -> list[dict]:
        """Return every non-expired promotion row (for the dashboard)."""
        now = self._now()
        rows = self._conn.execute(
            "SELECT * FROM promotions "
            "WHERE expires_at IS NULL OR expires_at > ? "
            "ORDER BY promoted_at DESC",
            (now,),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def expire_promotions(self) -> int:
        """Delete promotions whose ``expires_at`` has passed.

        Returns:
            Number of expired rows removed.
        """
        now = self._now()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "DELETE FROM promotions "
                "WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (now,),
            )
            self._conn.commit()
            return cur.rowcount

    def demote_symbol(self, ticker: str) -> None:
        """Remove *ticker* from the promotion table (manual demotion)."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM promotions WHERE ticker = ?", (ticker.upper(),)
            )
            self._conn.commit()


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------

_INSTANCE: Dict[str, UniverseDB] = {}
_INSTANCE_LOCK = threading.Lock()


def get_universe_db(data_dir: str | Path) -> UniverseDB:
    """Return a per-``data_dir`` cached :class:`UniverseDB` singleton.

    Args:
        data_dir: Directory that contains (or will contain) ``universe.db``.
            Typically ``settings.DATA_DIR``.

    Returns:
        Shared :class:`UniverseDB` instance.
    """
    key = str(Path(data_dir).resolve())
    with _INSTANCE_LOCK:
        db = _INSTANCE.get(key)
        if db is None:
            db = UniverseDB(Path(data_dir) / _DB_FILENAME)
            _INSTANCE[key] = db
        return db


def db_exists(data_dir: str | Path) -> bool:
    """Check whether ``universe.db`` already exists on disk.

    Useful for fallback logic — if the database has never been seeded the
    engine should fall back to the hard-coded universe in
    :mod:`config.universe`.

    Args:
        data_dir: Directory to check.

    Returns:
        *True* if the file exists and is non-empty.
    """
    p = Path(data_dir) / _DB_FILENAME
    return p.exists() and p.stat().st_size > 0
