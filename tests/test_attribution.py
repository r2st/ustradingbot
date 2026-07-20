"""Tests for performance attribution + scheduled statements (P1-10)."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from analytics.attribution import (
    factor_attribution,
    sector_attribution,
    strategy_attribution,
)
from automation.scheduler import Job, is_due
from automation.statements import build_statement
from config.settings import Settings


def _trades() -> pd.DataFrame:
    # AAPL/MSFT Technology; JPM Financials.
    return pd.DataFrame([
        {"symbol": "AAPL", "strategy": "momentum", "pnl_net": 300.0,
         "r_multiple": 1.5, "exit_time": "2024-03-01"},
        {"symbol": "MSFT", "strategy": "swing", "pnl_net": -100.0,
         "r_multiple": -0.5, "exit_time": "2024-03-05"},
        {"symbol": "JPM", "strategy": "momentum", "pnl_net": 200.0,
         "r_multiple": 1.0, "exit_time": "2024-03-10"},
    ])


class TestSectorAttribution:
    def test_groups_by_sector(self) -> None:
        rows = sector_attribution(_trades())
        by = {r["sector"]: r for r in rows}
        assert by["Technology"]["total_pnl"] == 200.0  # 300 - 100
        assert by["Financials"]["total_pnl"] == 200.0
        # Contribution shares sum meaningfully (total P&L = 400).
        assert by["Technology"]["contribution_pct"] == 50.0

    def test_empty(self) -> None:
        assert sector_attribution(pd.DataFrame()) == []


class TestStrategyAttribution:
    def test_groups_by_strategy(self) -> None:
        rows = strategy_attribution(_trades())
        by = {r["strategy"]: r for r in rows}
        assert by["momentum"]["total_pnl"] == 500.0  # AAPL + JPM
        assert by["swing"]["total_pnl"] == -100.0
        assert "contribution_pct" in by["momentum"]


class TestFactorAttribution:
    def test_beta_of_scaled_market(self) -> None:
        rng = np.random.default_rng(1)
        market = pd.Series(rng.normal(0.001, 0.02, 100))
        port = market * 1.5  # beta 1.5, no alpha
        out = factor_attribution(port, market, total_pnl=1000.0)
        assert out["beta"] == pytest.approx(1.5, abs=0.01)
        assert out["observations"] == 100

    def test_insufficient_data(self) -> None:
        out = factor_attribution([0.01], [0.01])
        assert out["beta"] is None
        assert out["observations"] <= 1


class TestScheduler:
    def test_monthly_due_on_day(self) -> None:
        job = Job("stmt", 8, 0, lambda: None, kind="monthly", day=1)
        now = datetime(2024, 3, 1, 8, 30)
        assert is_due(job, now, None) is True
        # Not the 1st -> not due.
        assert is_due(job, datetime(2024, 3, 2, 8, 30), None) is False

    def test_monthly_once_per_month(self) -> None:
        job = Job("stmt", 8, 0, lambda: None, kind="monthly", day=1)
        last = datetime(2024, 3, 1, 8, 5)
        # Same month -> not due again.
        assert is_due(job, datetime(2024, 3, 1, 9, 0), last) is False
        # Next month -> due.
        assert is_due(job, datetime(2024, 4, 1, 8, 5), last) is True

    def test_quarterly_months_filter(self) -> None:
        job = Job("stmt", 8, 0, lambda: None, kind="monthly", day=1,
                  months=frozenset({1, 4, 7, 10}))
        assert is_due(job, datetime(2024, 4, 1, 8, 5), None) is True
        assert is_due(job, datetime(2024, 3, 1, 8, 5), None) is False

    def test_day_clamped_to_month_length(self) -> None:
        # Day 31 job should fire on Feb 29 (2024 leap) since Feb has no 31st.
        job = Job("stmt", 8, 0, lambda: None, kind="monthly", day=31)
        assert is_due(job, datetime(2024, 2, 29, 8, 5), None) is True


class TestStatement:
    def test_build_statement_produces_pdf(self, tmp_path) -> None:
        # Write a minimal trades.csv so the report builds.
        _trades().to_csv(tmp_path / "trades.csv", index=False)
        settings = Settings(DATA_DIR=tmp_path)
        stmt = build_statement(settings, period="monthly",
                               now=datetime(2024, 3, 31))
        assert "Statement" in stmt.subject
        assert stmt.body
        assert stmt.pdf[:4] == b"%PDF"  # a real PDF header

    def test_quarterly_label(self, tmp_path) -> None:
        _trades().to_csv(tmp_path / "trades.csv", index=False)
        settings = Settings(DATA_DIR=tmp_path)
        stmt = build_statement(settings, period="quarterly")
        assert stmt.period == "quarterly"
        assert "Quarterly" in stmt.subject
