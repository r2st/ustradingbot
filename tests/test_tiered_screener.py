"""Tests for signals.screener — parallel scanning and tiered scan helpers."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from signals.screener import (
    _grade_meets_minimum,
    run_full_scan,
    run_prescreen,
    run_sector_scan,
)
from signals.signal_types import Grade


# ---------------------------------------------------------------------------
# _grade_meets_minimum
# ---------------------------------------------------------------------------


class TestGradeMeetsMinimum:
    def test_a_meets_b(self) -> None:
        assert _grade_meets_minimum(Grade.A, "B") is True

    def test_b_meets_b(self) -> None:
        assert _grade_meets_minimum(Grade.B, "B") is True

    def test_c_does_not_meet_b(self) -> None:
        assert _grade_meets_minimum(Grade.C, "B") is False

    def test_f_never_meets(self) -> None:
        assert _grade_meets_minimum(Grade.F, "C") is False

    def test_a_meets_a(self) -> None:
        assert _grade_meets_minimum(Grade.A, "A") is True


# ---------------------------------------------------------------------------
# run_full_scan — parallel vs sequential
# ---------------------------------------------------------------------------


class TestRunFullScan:
    def test_empty_symbols(self) -> None:
        signals = run_full_scan([], min_grade="B")
        assert signals == []

    @patch("signals.screener._scan_symbol", return_value=None)
    def test_sequential_when_workers_1(self, mock_scan: MagicMock) -> None:
        """With max_workers=1, uses sequential loop."""
        run_full_scan(["AAPL", "MSFT"], min_grade="B", max_workers=1)
        assert mock_scan.call_count == 2

    @patch("signals.screener._scan_symbol", return_value=None)
    def test_parallel_when_workers_gt_1(self, mock_scan: MagicMock) -> None:
        """With max_workers>1, uses ThreadPoolExecutor (calls still happen)."""
        run_full_scan(
            ["AAPL", "MSFT", "GOOG", "JPM"],
            min_grade="B",
            max_workers=2,
        )
        assert mock_scan.call_count == 4

    @patch("signals.screener._scan_symbol", side_effect=Exception("boom"))
    def test_errors_counted_not_raised(self, mock_scan: MagicMock) -> None:
        """Exceptions in individual scans are caught, not propagated."""
        signals = run_full_scan(["AAPL"], min_grade="B", max_workers=1)
        assert signals == []


# ---------------------------------------------------------------------------
# run_sector_scan
# ---------------------------------------------------------------------------


class TestRunSectorScan:
    @patch("signals.screener.run_full_scan", return_value=[])
    @patch("config.universe.get_tier2_symbols", return_value=["AAPL", "MSFT"])
    def test_calls_full_scan_with_sector_symbols(
        self, mock_tier2: MagicMock, mock_scan: MagicMock,
    ) -> None:
        result = run_sector_scan("Technology")
        mock_tier2.assert_called_once_with("Technology")
        mock_scan.assert_called_once()
        call_args = mock_scan.call_args
        assert call_args[0][0] == ["AAPL", "MSFT"]

    @patch("config.universe.get_tier2_symbols", return_value=[])
    def test_empty_sector_returns_empty(self, mock_tier2: MagicMock) -> None:
        result = run_sector_scan("EmptySector")
        assert result == []


# ---------------------------------------------------------------------------
# run_prescreen
# ---------------------------------------------------------------------------


class TestRunPrescreen:
    def test_empty_symbols(self) -> None:
        result = run_prescreen([], price_change_pct=0.03)
        assert result == []

    @patch("signals.screener.fetch_ohlcv", return_value=None)
    def test_no_data_returns_empty(self, mock_fetch: MagicMock) -> None:
        result = run_prescreen(["AAPL"], max_workers=1)
        assert result == []

    @patch("signals.screener.fetch_ohlcv")
    def test_qualifying_by_price_change(self, mock_fetch: MagicMock) -> None:
        """Symbol with 5% daily move qualifies."""
        import pandas as pd

        df = pd.DataFrame({
            "Close": [100.0, 105.0],
            "Volume": [1_000_000, 1_000_000],
        })
        mock_fetch.return_value = df
        result = run_prescreen(["AAPL"], price_change_pct=0.03, max_workers=1)
        assert "AAPL" in result

    @patch("signals.screener.fetch_ohlcv")
    def test_qualifying_by_volume_spike(self, mock_fetch: MagicMock) -> None:
        """Symbol with high volume spike qualifies."""
        import pandas as pd

        # mean vol = (500k + 500k + 500k + 500k + 5M) / 5 = 1.3M
        # latest vol 5M / mean 1.3M = 3.85x > 2.0 threshold
        df = pd.DataFrame({
            "Close": [100.0, 100.1, 100.2, 100.1, 100.15],  # tiny moves
            "Volume": [500_000, 500_000, 500_000, 500_000, 5_000_000],
        })
        mock_fetch.return_value = df
        result = run_prescreen(
            ["MSFT"], price_change_pct=0.03, volume_ratio=2.0, max_workers=1,
        )
        assert "MSFT" in result

    @patch("signals.screener.fetch_ohlcv")
    def test_non_qualifying_excluded(self, mock_fetch: MagicMock) -> None:
        """Symbol with flat price and normal volume doesn't qualify."""
        import pandas as pd

        df = pd.DataFrame({
            "Close": [100.0, 100.1],  # 0.1% move
            "Volume": [1_000_000, 1_100_000],  # 1.1x
        })
        mock_fetch.return_value = df
        result = run_prescreen(
            ["FLAT"], price_change_pct=0.03, volume_ratio=2.0, max_workers=1,
        )
        assert result == []
