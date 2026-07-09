"""Tests for config.universe updates — DB-aware sector/tier lookups with fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.universe import (
    ALL_SYMBOLS,
    SECTOR_BY_SYMBOL,
    get_all_sectors,
    get_currency,
    get_sector,
    get_tier1_symbols,
    get_tier2_symbols,
    get_tier3_symbols,
    is_canadian,
)


# ---------------------------------------------------------------------------
# Existing behaviour (no universe DB)
# ---------------------------------------------------------------------------


class TestGetSectorFallback:
    def test_known_symbol(self) -> None:
        assert get_sector("AAPL") == "Technology"
        assert get_sector("JPM") == "Financials"

    def test_unknown_defaults_to_unknown(self) -> None:
        assert get_sector("ZZZZZZ") == "Unknown"


class TestIsCanadian:
    def test_to_suffix(self) -> None:
        assert is_canadian("SHOP.TO") is True

    def test_v_suffix(self) -> None:
        assert is_canadian("XYZ.V") is True

    def test_us_stock(self) -> None:
        assert is_canadian("AAPL") is False


class TestGetCurrency:
    def test_cad_for_toronto(self) -> None:
        assert get_currency("ENB.TO") == "CAD"

    def test_cad_for_tsxv(self) -> None:
        assert get_currency("XYZ.V") == "CAD"

    def test_usd_for_us(self) -> None:
        assert get_currency("AAPL") == "USD"


class TestGetAllSectors:
    def test_returns_multiple(self) -> None:
        sectors = get_all_sectors()
        assert len(sectors) >= 5
        sector_names = {s["sector"] for s in sectors}
        assert "Technology" in sector_names
        assert "Energy" in sector_names


class TestTierFallback:
    """Without a universe.db, tier functions fall back to hardcoded lists."""

    def test_tier1_returns_all_symbols(self) -> None:
        tier1 = get_tier1_symbols()
        assert set(tier1) == set(ALL_SYMBOLS)

    def test_tier2_returns_sector_symbols(self) -> None:
        tech = get_tier2_symbols("Technology")
        expected = [s for s, sec in SECTOR_BY_SYMBOL.items() if sec == "Technology"]
        assert set(tech) == set(expected)

    def test_tier2_empty_sector(self) -> None:
        result = get_tier2_symbols("NonexistentSector")
        assert result == []

    def test_tier3_returns_all_symbols(self) -> None:
        tier3 = get_tier3_symbols()
        assert set(tier3) == set(ALL_SYMBOLS)
