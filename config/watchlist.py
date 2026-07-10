"""
User-managed watchlists — add/remove symbols and organise them into named
lists (e.g. "Technology", "Energy") that persist to a JSON file.

The engine scans the union of every *enabled* list instead of the hard-coded
:data:`config.universe.ALL_SYMBOLS`.  On first use the store seeds itself from
the built-in universe (grouped by sector) so behaviour is unchanged until the
user starts editing their lists from the dashboard.

The on-disk format (``DATA_DIR/watchlists.json``)::

    {
      "lists": {
        "Technology": {"symbols": ["AAPL", "MSFT"], "enabled": true},
        "Energy":     {"symbols": ["ENB.TO", "SU.TO"], "enabled": true}
      }
    }

All mutations are validated (symbols upper-cased and pattern-checked) and
written atomically (temp file + ``os.replace``) so a crash mid-write can never
corrupt the file.  A :class:`WatchlistError` is raised for invalid input.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from threading import RLock
from typing import Dict, List

from config.universe import ALL_SYMBOLS, SECTOR_BY_SYMBOL, get_sector

# A permissive-but-safe ticker pattern: 1-6 alphanumerics, optionally a
# ``.XX`` exchange suffix (e.g. ``SHOP.TO``).  Guards against junk / injection.
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,6}(\.[A-Z]{1,3})?$")

_FILENAME = "watchlists.json"


class WatchlistError(ValueError):
    """Raised when a watchlist operation gets invalid input."""


def normalize_symbol(symbol: str) -> str:
    """Upper-case, strip, and validate a ticker symbol.

    Raises:
        WatchlistError: if the symbol is empty or malformed.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise WatchlistError("Symbol must not be empty.")
    if not _SYMBOL_RE.match(sym):
        raise WatchlistError(f"Invalid symbol: {symbol!r}")
    return sym


def _default_lists() -> Dict[str, Dict]:
    """Seed named lists from the built-in universe, grouped by sector.

    Adds a dedicated **"ETFs"** list (broad-market + sector SPDR ETFs) so ETF
    support is available out of the box; the ``_SYMBOL_RE`` pattern already
    accepts these tickers.
    """
    by_sector: Dict[str, List[str]] = {}
    for sym in ALL_SYMBOLS:
        sector = SECTOR_BY_SYMBOL.get(sym, get_sector(sym))
        by_sector.setdefault(sector, []).append(sym)
    lists: Dict[str, Dict] = {
        sector: {"symbols": sorted(syms), "enabled": True}
        for sector, syms in sorted(by_sector.items())
    }
    try:
        from config.etf_universe import ALL_ETFS

        lists["ETFs"] = {"symbols": sorted(ALL_ETFS), "enabled": True}
    except Exception:  # noqa: BLE001 — ETF seeding is best-effort
        pass
    return lists


class WatchlistStore:
    """Thread-safe, file-backed store of named symbol watchlists.

    Args:
        data_dir: Directory holding ``watchlists.json``.  Created if absent.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / _FILENAME
        self._lock = RLock()
        self._lists: Dict[str, Dict] = {}
        self._load()

    # ------------------------------------------------------------------ io

    def _load(self) -> None:
        with self._lock:
            if not self._path.exists():
                self._lists = _default_lists()
                self._save()
                return
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                lists = raw.get("lists", {}) if isinstance(raw, dict) else {}
            except (json.JSONDecodeError, OSError):
                lists = {}
            cleaned: Dict[str, Dict] = {}
            for name, entry in lists.items():
                if not isinstance(entry, dict):
                    continue
                syms = entry.get("symbols", [])
                if not isinstance(syms, list):
                    continue
                valid: List[str] = []
                for s in syms:
                    try:
                        valid.append(normalize_symbol(s))
                    except WatchlistError:
                        continue
                cleaned[str(name)] = {
                    "symbols": sorted(dict.fromkeys(valid)),
                    "enabled": bool(entry.get("enabled", True)),
                }
            self._lists = cleaned or _default_lists()
            if not cleaned:
                self._save()

    def _save(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"lists": self._lists}, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self._data_dir), prefix=".wl_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, str(self._path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -------------------------------------------------------------- queries

    def as_dict(self) -> Dict[str, Dict]:
        """Return a deep-ish copy of every list: ``{name: {symbols, enabled}}``."""
        with self._lock:
            return {
                name: {"symbols": list(e["symbols"]), "enabled": e["enabled"]}
                for name, e in self._lists.items()
            }

    def list_names(self) -> List[str]:
        with self._lock:
            return sorted(self._lists.keys())

    def scan_symbols(self) -> List[str]:
        """Return the sorted, de-duplicated union of every *enabled* list."""
        with self._lock:
            out: set[str] = set()
            for entry in self._lists.values():
                if entry.get("enabled", True):
                    out.update(entry["symbols"])
            return sorted(out)

    def all_symbols(self) -> List[str]:
        """Return every symbol across all lists (enabled or not)."""
        with self._lock:
            out: set[str] = set()
            for entry in self._lists.values():
                out.update(entry["symbols"])
            return sorted(out)

    # ------------------------------------------------------------ mutations

    def create_list(self, name: str) -> None:
        name = str(name or "").strip()
        if not name:
            raise WatchlistError("List name must not be empty.")
        with self._lock:
            if name in self._lists:
                raise WatchlistError(f"List {name!r} already exists.")
            self._lists[name] = {"symbols": [], "enabled": True}
            self._save()

    def delete_list(self, name: str) -> None:
        with self._lock:
            if name not in self._lists:
                raise WatchlistError(f"Unknown list {name!r}.")
            del self._lists[name]
            self._save()

    def set_enabled(self, name: str, enabled: bool) -> None:
        with self._lock:
            if name not in self._lists:
                raise WatchlistError(f"Unknown list {name!r}.")
            self._lists[name]["enabled"] = bool(enabled)
            self._save()

    def add_symbol(self, name: str, symbol: str) -> str:
        """Add *symbol* to list *name* (creating the list if it doesn't exist).

        Returns the normalised symbol that was added.
        """
        sym = normalize_symbol(symbol)
        with self._lock:
            entry = self._lists.setdefault(name, {"symbols": [], "enabled": True})
            if sym not in entry["symbols"]:
                entry["symbols"] = sorted([*entry["symbols"], sym])
                self._save()
            return sym

    def remove_symbol(self, name: str, symbol: str) -> str:
        sym = normalize_symbol(symbol)
        with self._lock:
            if name not in self._lists:
                raise WatchlistError(f"Unknown list {name!r}.")
            entry = self._lists[name]
            if sym in entry["symbols"]:
                entry["symbols"] = [s for s in entry["symbols"] if s != sym]
                self._save()
            return sym


# ---------------------------------------------------------------------------
# Process-wide accessor
# ---------------------------------------------------------------------------

_STORE: Dict[str, WatchlistStore] = {}
_STORE_LOCK = RLock()


def get_watchlist_store(data_dir: str | Path) -> WatchlistStore:
    """Return a per-``data_dir`` cached :class:`WatchlistStore` singleton."""
    key = str(Path(data_dir).resolve())
    with _STORE_LOCK:
        store = _STORE.get(key)
        if store is None:
            store = WatchlistStore(data_dir)
            _STORE[key] = store
        return store


def scan_symbols_for(settings) -> List[str]:
    """Return the symbols the engine should scan for *settings*.

    When the universe database exists and tiered scanning is enabled, returns
    the Tier 1 watchlist from the database.  Otherwise falls back to the
    JSON watchlist file, then to the hard-coded universe.
    """
    # Try universe DB first (Full Stock Universe feature).
    try:
        from data_store.universe import db_exists, get_universe_db

        if db_exists(settings.DATA_DIR):
            db = get_universe_db(settings.DATA_DIR)
            tier1 = db.get_tier1_symbols()
            if tier1:
                return tier1
    except Exception:  # noqa: BLE001 -- never break the scan on DB issues
        pass

    if not getattr(settings, "USE_WATCHLIST_FILE", True):
        return list(ALL_SYMBOLS)
    try:
        symbols = get_watchlist_store(settings.DATA_DIR).scan_symbols()
    except Exception:  # noqa: BLE001 -- never break the scan on a bad file
        return list(ALL_SYMBOLS)
    return symbols or list(ALL_SYMBOLS)
