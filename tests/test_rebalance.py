"""Tests for portfolio rebalancing / target allocation (P1-8)."""

from __future__ import annotations

import pytest

from analytics.rebalance import (
    build_rebalance_report,
    compute_drift,
    current_allocation,
    rebalance_suggestions,
)


def _pos(symbol, strategy, price, qty):
    return {"symbol": symbol, "strategy": strategy, "entry_price": price,
            "quantity": qty}


# AAPL/MSFT are Technology; JPM is Financials (per config.universe).
_BOOK = [
    _pos("AAPL", "momentum", 100, 60),   # 6000 tech
    _pos("MSFT", "swing", 100, 20),      # 2000 tech
    _pos("JPM", "momentum", 100, 20),    # 2000 financials
]  # total 10_000; tech 80%, financials 20%


class TestAllocation:
    def test_by_sector(self) -> None:
        alloc = current_allocation(_BOOK, dimension="sector")
        assert alloc["total"] == 10_000
        assert alloc["buckets"]["Technology"]["pct"] == pytest.approx(0.80)
        assert alloc["buckets"]["Financials"]["pct"] == pytest.approx(0.20)

    def test_by_strategy(self) -> None:
        alloc = current_allocation(_BOOK, dimension="strategy")
        assert alloc["buckets"]["momentum"]["value"] == 8000  # AAPL + JPM
        assert alloc["buckets"]["swing"]["value"] == 2000

    def test_marks_to_prices(self) -> None:
        alloc = current_allocation(
            _BOOK, dimension="sector", prices={"AAPL": 200.0}
        )
        # AAPL now 200*60=12000 -> total 16000.
        assert alloc["total"] == 16_000

    def test_invalid_dimension(self) -> None:
        with pytest.raises(ValueError):
            current_allocation(_BOOK, dimension="bogus")


class TestDrift:
    def test_drift_points(self) -> None:
        alloc = current_allocation(_BOOK, dimension="sector")
        drift = compute_drift(alloc, {"Technology": 50, "Financials": 50})
        by = {r["bucket"]: r for r in drift}
        assert by["Technology"]["drift_pct"] == pytest.approx(30.0)   # 80 - 50
        assert by["Financials"]["drift_pct"] == pytest.approx(-30.0)  # 20 - 50

    def test_target_with_no_holding(self) -> None:
        alloc = current_allocation(_BOOK, dimension="sector")
        drift = compute_drift(alloc, {"Technology": 50, "Energy": 50})
        by = {r["bucket"]: r for r in drift}
        assert by["Energy"]["current_pct"] == 0.0
        assert by["Energy"]["drift_pct"] == pytest.approx(-50.0)

    def test_targets_accept_fractions_or_percent(self) -> None:
        alloc = current_allocation(_BOOK, dimension="sector")
        d1 = compute_drift(alloc, {"Technology": 0.5, "Financials": 0.5})
        d2 = compute_drift(alloc, {"Technology": 50, "Financials": 50})
        assert {r["bucket"]: r["drift_pct"] for r in d1} == \
               {r["bucket"]: r["drift_pct"] for r in d2}


class TestSuggestions:
    def test_trim_and_add(self) -> None:
        alloc = current_allocation(_BOOK, dimension="sector")
        actions = rebalance_suggestions(
            alloc, {"Technology": 50, "Financials": 50}, drift_threshold_pct=5
        )
        by = {a.bucket: a for a in actions}
        # Tech over-allocated (80% vs 50%) -> trim; target value 5000, cur 8000.
        assert by["Technology"].action == "trim"
        assert by["Technology"].delta_value == -3000.0
        assert by["Financials"].action == "add"
        assert by["Financials"].delta_value == 3000.0

    def test_threshold_suppresses_small_drift(self) -> None:
        alloc = current_allocation(_BOOK, dimension="sector")
        # Targets close to current -> no suggestions past a 5pp threshold.
        actions = rebalance_suggestions(
            alloc, {"Technology": 78, "Financials": 22}, drift_threshold_pct=5
        )
        assert actions == []


class TestReport:
    def test_full_report(self) -> None:
        report = build_rebalance_report(
            _BOOK, {"Technology": 50, "Financials": 50},
            dimension="sector", drift_threshold_pct=5,
        )
        assert report["needs_rebalance"] is True
        assert report["total_value"] == 10_000
        assert report["suggestions"]
        assert "Technology" in report["allocation"]

    def test_balanced_report(self) -> None:
        report = build_rebalance_report(
            _BOOK, {"Technology": 80, "Financials": 20},
            dimension="sector", drift_threshold_pct=5,
        )
        assert report["needs_rebalance"] is False
        assert report["suggestions"] == []
