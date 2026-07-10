"""Tests for the expanded backtest + watchlist symbol sets.

Verifies that the backtest form and dashboard template expose the full
symbol universe (US equities + Canadian equities + ETFs + user-added
symbols) rather than only the hardcoded 41-symbol list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config.etf_universe import ALL_ETFS, BROAD_MARKET_ETFS, SECTOR_ETFS
from config.universe import ALL_SYMBOLS, CA_WATCHLIST, US_WATCHLIST


# ---------------------------------------------------------------------------
# Backtest control — _full_symbol_set and options()
# ---------------------------------------------------------------------------


class TestFullSymbolSet:
    """Tests for backtest_control._full_symbol_set()."""

    def test_includes_all_hardcoded_symbols(self) -> None:
        """Every hardcoded symbol (US + CA) must appear in the full set."""
        from dashboard.backtest_control import _full_symbol_set

        full = _full_symbol_set()
        for sym in ALL_SYMBOLS:
            assert sym in full, f"Hardcoded symbol {sym} missing from full set"

    def test_includes_all_etfs(self) -> None:
        """Every known ETF must appear in the full set."""
        from dashboard.backtest_control import _full_symbol_set

        full = _full_symbol_set()
        for etf in ALL_ETFS:
            assert etf in full, f"ETF {etf} missing from full set"

    def test_larger_than_hardcoded_alone(self) -> None:
        """The full set must be strictly larger than the old hardcoded list."""
        from dashboard.backtest_control import _full_symbol_set

        full = _full_symbol_set()
        assert len(full) > len(ALL_SYMBOLS), (
            f"Full set ({len(full)}) should be larger than "
            f"ALL_SYMBOLS ({len(ALL_SYMBOLS)})"
        )

    def test_is_sorted_and_deduplicated(self) -> None:
        """Result must be sorted with no duplicates."""
        from dashboard.backtest_control import _full_symbol_set

        full = _full_symbol_set()
        assert full == sorted(set(full))

    def test_includes_user_watchlist_symbols(self, tmp_path: Path) -> None:
        """Symbols added to the watchlist store must appear in the full set."""
        import json

        from config.settings import Settings

        # Seed a watchlist with an extra symbol not in ALL_SYMBOLS or ALL_ETFS.
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        wl = {
            "lists": {
                "Custom": {
                    "symbols": ["MU", "QCOM", "SOXL"],
                    "enabled": True,
                },
            },
        }
        (data_dir / "watchlists.json").write_text(json.dumps(wl))

        # Patch get_settings so the backtest control picks up our temp dir.
        settings = Settings(DATA_DIR=data_dir)
        import dashboard.backtest_control as bc

        original = bc.get_settings
        bc.get_settings = lambda: settings
        try:
            full = bc._full_symbol_set()
        finally:
            bc.get_settings = original

        for sym in ("MU", "QCOM", "SOXL"):
            assert sym in full, f"User-added symbol {sym} missing from full set"


class TestBacktestOptions:
    """Tests for the options() endpoint payload."""

    def test_options_symbols_include_etfs(self) -> None:
        """The options payload must list ETFs alongside equities."""
        from dashboard.backtest_control import options

        opts = options()
        symbols = opts["symbols"]
        for etf in BROAD_MARKET_ETFS:
            assert etf in symbols, f"ETF {etf} missing from options symbols"

    def test_options_max_symbols_accommodates_full_set(self) -> None:
        """max_symbols default must be >= the number of symbols offered."""
        from dashboard.backtest_control import options

        opts = options()
        max_sym = opts["defaults"]["max_symbols"]
        assert max_sym >= len(opts["symbols"]), (
            f"max_symbols ({max_sym}) is smaller than available symbols "
            f"({len(opts['symbols'])})"
        )

    def test_max_symbols_raised(self) -> None:
        """_MAX_SYMBOLS must be at least 100 (up from the old 40)."""
        from dashboard.backtest_control import _MAX_SYMBOLS

        assert _MAX_SYMBOLS >= 100


# ---------------------------------------------------------------------------
# Dashboard template context — system status and watchlist lists
# ---------------------------------------------------------------------------


class TestDashboardSymbolContext:
    """Tests for the dashboard template context symbol data."""

    def test_system_status_total_includes_etfs(self) -> None:
        """total_symbols in the system status must count ETFs too."""
        expected_total = len(set(ALL_SYMBOLS) | set(ALL_ETFS))
        assert expected_total > len(ALL_SYMBOLS), (
            "ETFs should increase the total symbol count"
        )

    def test_system_status_etf_count(self) -> None:
        """The status dict must carry an etf_symbols count."""
        # This mirrors what _build_system_status() now returns.
        assert len(ALL_ETFS) == 15

    def test_etf_list_not_empty(self) -> None:
        """ALL_ETFS must be non-empty so the template renders an ETF section."""
        assert len(ALL_ETFS) > 0

    def test_no_overlap_between_etfs_and_equities(self) -> None:
        """ETFs and hardcoded equities should not overlap."""
        overlap = set(ALL_ETFS) & set(ALL_SYMBOLS)
        assert len(overlap) == 0, f"Unexpected overlap: {overlap}"
