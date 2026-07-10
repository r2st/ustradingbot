"""Tests for config.index_membership — S&P 500 / NASDAQ-100 data source."""

from __future__ import annotations

from unittest.mock import patch

from config import index_membership as im


class TestStaticLists:
    def test_sp500_is_substantial(self) -> None:
        symbols = im.sp500_symbols()
        # A real large/mid-cap S&P 500 core — hundreds of names, no dupes.
        assert len(symbols) >= 400
        assert len(symbols) == len(set(symbols))
        assert symbols == sorted(symbols)

    def test_nasdaq100_is_around_100(self) -> None:
        symbols = im.nasdaq100_symbols()
        assert 90 <= len(symbols) <= 110
        assert len(symbols) == len(set(symbols))

    def test_class_shares_use_hyphen(self) -> None:
        # Provider (yfinance/Alpaca) form, not the Wikipedia dot form.
        assert "BRK-B" in im.sp500_symbols()
        assert "BRK.B" not in im.sp500_symbols()

    def test_index_universe_is_union(self) -> None:
        union = set(im.index_universe())
        assert union == set(im.sp500_symbols()) | set(im.nasdaq100_symbols())
        assert im.index_universe() == sorted(union)


class TestMembershipHelpers:
    def test_in_sp500(self) -> None:
        assert im.in_sp500("AAPL") is True
        assert im.in_sp500("aapl") is True  # case-insensitive
        assert im.in_sp500("ZZZZ") is False

    def test_in_nasdaq100(self) -> None:
        assert im.in_nasdaq100("NVDA") is True
        assert im.in_nasdaq100("XOM") is False  # NYSE energy, not in NDX

    def test_indices_for_both(self) -> None:
        assert im.indices_for("AAPL") == ["SP500", "NASDAQ100"]

    def test_indices_for_sp500_only(self) -> None:
        assert im.indices_for("XOM") == ["SP500"]

    def test_indices_for_neither(self) -> None:
        assert im.indices_for("ZZZZ") == []

    def test_static_symbols_dispatch(self) -> None:
        assert im.static_symbols(im.SP500_INDEX) == im.sp500_symbols()
        assert im.static_symbols(im.NASDAQ100_INDEX) == im.nasdaq100_symbols()
        assert im.static_symbols("BOGUS") == []


class TestNormalize:
    def test_dot_to_hyphen(self) -> None:
        assert im._normalize("BRK.B") == "BRK-B"

    def test_plain_passthrough(self) -> None:
        assert im._normalize("AAPL") == "AAPL"


class TestFetchConstituents:
    def test_unknown_index_returns_static(self) -> None:
        assert im.fetch_index_constituents("BOGUS") == []

    def test_fetch_success_normalizes(self) -> None:
        import pandas as pd

        table = pd.DataFrame({"Symbol": ["AAPL", "BRK.B", "MSFT"] + [f"S{i}" for i in range(50)]})
        with patch("pandas.read_html", return_value=[table]):
            result = im.fetch_index_constituents(im.SP500_INDEX)
        assert "BRK-B" in result
        assert "AAPL" in result
        assert result == sorted(set(result))

    def test_fetch_failure_falls_back_to_static(self) -> None:
        with patch("pandas.read_html", side_effect=RuntimeError("network down")):
            result = im.fetch_index_constituents(im.NASDAQ100_INDEX)
        assert result == im.nasdaq100_symbols()

    def test_fetch_tiny_table_falls_back(self) -> None:
        import pandas as pd

        # Fewer than the 50-row sanity floor -> treated as not a real table.
        table = pd.DataFrame({"Symbol": ["AAPL", "MSFT"]})
        with patch("pandas.read_html", return_value=[table]):
            result = im.fetch_index_constituents(im.SP500_INDEX)
        assert result == im.sp500_symbols()
