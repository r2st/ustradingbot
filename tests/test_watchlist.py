"""Tests for the user-managed watchlist store and engine integration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config.settings import Settings
from config.watchlist import (
    WatchlistError,
    WatchlistStore,
    get_watchlist_store,
    normalize_symbol,
    scan_symbols_for,
)


def test_normalize_symbol_valid():
    assert normalize_symbol(" aapl ") == "AAPL"
    assert normalize_symbol("shop.to") == "SHOP.TO"


@pytest.mark.parametrize("bad", ["", "  ", "TOOLONGSYM", "AA PL", "A;DROP", "123456789"])
def test_normalize_symbol_invalid(bad):
    with pytest.raises(WatchlistError):
        normalize_symbol(bad)


def test_seeds_from_universe(tmp_data_dir: Path):
    store = WatchlistStore(tmp_data_dir)
    assert (tmp_data_dir / "watchlists.json").exists()
    # Seeded lists should cover the whole universe.
    assert "AAPL" in store.scan_symbols()
    assert len(store.list_names()) > 1  # grouped by sector


def test_add_and_remove_symbol(tmp_data_dir: Path):
    store = WatchlistStore(tmp_data_dir)
    store.create_list("My List")
    store.add_symbol("My List", "tsla")
    assert "TSLA" in store.as_dict()["My List"]["symbols"]
    store.remove_symbol("My List", "TSLA")
    assert "TSLA" not in store.as_dict()["My List"]["symbols"]


def test_add_symbol_dedups_and_sorts(tmp_data_dir: Path):
    store = WatchlistStore(tmp_data_dir)
    store.create_list("L")
    store.add_symbol("L", "MSFT")
    store.add_symbol("L", "AAPL")
    store.add_symbol("L", "aapl")  # duplicate
    assert store.as_dict()["L"]["symbols"] == ["AAPL", "MSFT"]


def test_disabled_list_excluded_from_scan(tmp_data_dir: Path):
    store = WatchlistStore(tmp_data_dir)
    store.create_list("Only")
    store.add_symbol("Only", "NVDA")
    # Disable every seeded list so "Only" is the sole enabled one.
    for name in store.list_names():
        store.set_enabled(name, name == "Only")
    assert store.scan_symbols() == ["NVDA"]
    store.set_enabled("Only", False)
    # With nothing enabled, scan set is empty (engine falls back to universe).
    assert store.scan_symbols() == []


def test_create_duplicate_list_raises(tmp_data_dir: Path):
    store = WatchlistStore(tmp_data_dir)
    store.create_list("Dup")
    with pytest.raises(WatchlistError):
        store.create_list("Dup")


def test_persistence_across_instances(tmp_data_dir: Path):
    store = WatchlistStore(tmp_data_dir)
    store.create_list("Persist")
    store.add_symbol("Persist", "COIN")
    reloaded = WatchlistStore(tmp_data_dir)
    assert "COIN" in reloaded.as_dict()["Persist"]["symbols"]


def test_corrupt_file_falls_back_to_defaults(tmp_data_dir: Path):
    (tmp_data_dir / "watchlists.json").write_text("{ not json", encoding="utf-8")
    store = WatchlistStore(tmp_data_dir)
    assert store.scan_symbols()  # non-empty defaults


def test_load_drops_invalid_symbols(tmp_data_dir: Path):
    (tmp_data_dir / "watchlists.json").write_text(
        json.dumps({"lists": {"X": {"symbols": ["AAPL", "bad sym", "MSFT"], "enabled": True}}}),
        encoding="utf-8",
    )
    store = WatchlistStore(tmp_data_dir)
    assert store.as_dict()["X"]["symbols"] == ["AAPL", "MSFT"]


def test_get_watchlist_store_is_cached(tmp_data_dir: Path):
    assert get_watchlist_store(tmp_data_dir) is get_watchlist_store(tmp_data_dir)


def test_scan_symbols_for_uses_watchlist(tmp_data_dir: Path):
    settings = Settings(DATA_DIR=tmp_data_dir, USE_WATCHLIST_FILE=True)
    store = get_watchlist_store(tmp_data_dir)
    for name in store.list_names():
        store.delete_list(name)
    store.create_list("Solo")
    store.add_symbol("Solo", "AAPL")
    assert scan_symbols_for(settings) == ["AAPL"]


def test_scan_symbols_for_disabled_uses_universe(tmp_data_dir: Path):
    from config.universe import ALL_SYMBOLS

    settings = Settings(DATA_DIR=tmp_data_dir, USE_WATCHLIST_FILE=False)
    assert scan_symbols_for(settings) == list(ALL_SYMBOLS)


def test_scan_symbols_for_empty_falls_back(tmp_data_dir: Path):
    from config.universe import ALL_SYMBOLS

    settings = Settings(DATA_DIR=tmp_data_dir, USE_WATCHLIST_FILE=True)
    store = get_watchlist_store(tmp_data_dir)
    for name in store.list_names():
        store.set_enabled(name, False)
    # Empty scan set -> fall back to the universe so the engine never idles.
    assert scan_symbols_for(settings) == list(ALL_SYMBOLS)
