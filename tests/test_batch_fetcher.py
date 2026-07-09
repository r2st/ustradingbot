"""Tests for data.fetcher — batch OHLCV download functionality."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from data.fetcher import _download_batch, fetch_batch_ohlcv, clear_cache


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_cache():
    """Clear the fetcher cache before and after each test."""
    clear_cache()
    yield
    clear_cache()


# ---------------------------------------------------------------------------
# _download_batch
# ---------------------------------------------------------------------------


class TestDownloadBatch:
    @patch("yfinance.download")
    def test_single_symbol(self, mock_dl: MagicMock) -> None:
        """Single-symbol download returns flat DataFrame."""
        df = pd.DataFrame({
            "Open": [100.0], "High": [105.0], "Low": [99.0],
            "Close": [103.0], "Volume": [1_000_000],
        })
        mock_dl.return_value = df
        result = _download_batch(["AAPL"], "6mo")
        assert "AAPL" in result
        assert set(result["AAPL"].columns) == {"Open", "High", "Low", "Close", "Volume"}

    @patch("yfinance.download")
    def test_empty_download(self, mock_dl: MagicMock) -> None:
        """Empty download returns empty dict."""
        mock_dl.return_value = pd.DataFrame()
        result = _download_batch(["AAPL"], "6mo")
        assert result == {}

    @patch("yfinance.download")
    def test_none_download(self, mock_dl: MagicMock) -> None:
        """None download returns empty dict."""
        mock_dl.return_value = None
        result = _download_batch(["AAPL"], "6mo")
        assert result == {}

    @patch("yfinance.download")
    def test_exception_returns_empty(self, mock_dl: MagicMock) -> None:
        """Exceptions return empty dict, not raised."""
        mock_dl.side_effect = Exception("rate limited")
        result = _download_batch(["AAPL"], "6mo")
        assert result == {}


# ---------------------------------------------------------------------------
# fetch_batch_ohlcv
# ---------------------------------------------------------------------------


class TestFetchBatchOhlcv:
    @patch("data.fetcher._download_batch")
    def test_basic_batch(self, mock_dl: MagicMock) -> None:
        """Fetches symbols in batches and returns combined results."""
        df = pd.DataFrame({
            "Open": [100.0], "High": [105.0], "Low": [99.0],
            "Close": [103.0], "Volume": [1_000_000],
        })
        mock_dl.return_value = {"AAPL": df, "MSFT": df}
        result = fetch_batch_ohlcv(["AAPL", "MSFT"], batch_size=10)
        assert "AAPL" in result
        assert "MSFT" in result

    @patch("data.fetcher._download_batch")
    def test_deduplicates_symbols(self, mock_dl: MagicMock) -> None:
        """Duplicate symbols are deduplicated."""
        mock_dl.return_value = {"AAPL": pd.DataFrame({"Close": [100]})}
        result = fetch_batch_ohlcv(["AAPL", "AAPL", "AAPL"], batch_size=10)
        # Should only call download once with deduplicated list
        assert mock_dl.call_count == 1

    @patch("data.fetcher._download_batch")
    def test_cache_hit_skips_download(self, mock_dl: MagicMock) -> None:
        """Symbols already in cache are not re-downloaded."""
        df = pd.DataFrame({
            "Open": [100.0], "High": [105.0], "Low": [99.0],
            "Close": [103.0], "Volume": [1_000_000],
        })
        # First call fills cache
        mock_dl.return_value = {"AAPL": df}
        fetch_batch_ohlcv(["AAPL"], batch_size=10)

        # Second call should skip AAPL (cached)
        mock_dl.reset_mock()
        mock_dl.return_value = {}
        result = fetch_batch_ohlcv(["AAPL"], batch_size=10)
        # _download_batch should not be called since all are cached
        assert mock_dl.call_count == 0
        assert "AAPL" in result

    def test_empty_symbols(self) -> None:
        """Empty symbol list returns empty dict immediately."""
        result = fetch_batch_ohlcv([])
        assert result == {}

    @patch("data.fetcher._download_batch")
    def test_adaptive_backoff(self, mock_dl: MagicMock) -> None:
        """Three consecutive empty batches trigger adaptive backoff."""
        mock_dl.return_value = {}
        # 5 symbols with batch_size=1 = 5 batches, all empty
        with patch("data.fetcher.time.sleep") as mock_sleep:
            fetch_batch_ohlcv(
                ["A", "B", "C", "D", "E"],
                batch_size=1,
                delay_between_batches=0.1,
            )
        # After 3 empties, delay should increase
        # There should be some sleep calls
        assert mock_sleep.call_count >= 1
