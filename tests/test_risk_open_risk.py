"""Tests for the F5 risk-dashboard enhancements (open risk, budget, MTM)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from analytics.risk_dashboard import (
    build_risk_report,
    daily_loss_budget,
    open_risk,
    portfolio_exposure,
)
from journal.trade_logger import SCHEMA_COLUMNS


def test_open_risk_long_and_marked(sample_signal):
    positions = [{"symbol": "NVDA", "entry_price": 100.0, "stop_price": 95.0,
                  "quantity": 10, "direction": "long"}]
    out = open_risk(positions, prices={"NVDA": 104.0}, total_capital=10_000.0)
    row = out["per_position"][0]
    # (current 104 − stop 95) × 10 = $90 at risk.
    assert row["risk_if_stopped"] == 90.0
    assert row["marked"] is True
    assert out["total"] == 90.0
    assert out["pct_of_capital"] == 0.009


def test_open_risk_trailed_stop_is_locked_profit():
    positions = [{"symbol": "WIN", "entry_price": 100.0, "stop_price": 108.0,
                  "quantity": 10, "direction": "long"}]
    out = open_risk(positions, prices={"WIN": 112.0}, total_capital=10_000.0)
    row = out["per_position"][0]
    # Stop above the mark → negative risk, flagged as locked profit,
    # never silently clamped to zero.
    assert row["risk_if_stopped"] == 40.0  # (112-108)*10 still at risk
    positions[0]["stop_price"] = 115.0     # stop ABOVE current price
    out = open_risk(positions, prices={"WIN": 112.0}, total_capital=10_000.0)
    row = out["per_position"][0]
    assert row["risk_if_stopped"] == -30.0
    assert row["locked_profit"] is True
    # Aggregate "total" sums positive risks only; "net" includes the lock.
    assert out["total"] == 0.0
    assert out["net"] == -30.0


def test_open_risk_short_mirrored():
    positions = [{"symbol": "SH", "entry_price": 100.0, "stop_price": 105.0,
                  "quantity": 10, "direction": "short"}]
    out = open_risk(positions, prices={"SH": 98.0}, total_capital=10_000.0)
    # Short: (stop 105 − current 98) × 10 = $70 at risk.
    assert out["per_position"][0]["risk_if_stopped"] == 70.0


def test_open_risk_falls_back_to_entry_without_quote():
    positions = [{"symbol": "NOQ", "entry_price": 100.0, "stop_price": 95.0,
                  "quantity": 10}]
    out = open_risk(positions, prices={}, total_capital=10_000.0)
    row = out["per_position"][0]
    assert row["risk_if_stopped"] == 50.0  # valued at entry
    assert row["marked"] is False


def test_daily_loss_budget_spec_example():
    # Spec: DAILY_LOSS_LIMIT_PCT=0.03, capital $100k, −$1,200 day → 40% used.
    b = daily_loss_budget(-1200.0, 100_000.0, 0.03)
    assert b["limit_usd"] == 3000.0
    assert b["used_today"] == 1200.0
    assert b["used_pct_of_budget"] == 0.4
    assert b["remaining"] == 1800.0
    # A profitable day uses none of the budget.
    assert daily_loss_budget(500.0, 100_000.0, 0.03)["used_today"] == 0.0


def test_exposure_market_value_and_fallback():
    positions = [
        {"symbol": "A", "entry_price": 100.0, "quantity": 10, "currency": "USD"},
        {"symbol": "B", "entry_price": 50.0, "quantity": 10, "currency": "USD"},
    ]
    out = portfolio_exposure(positions, {"USD": 10_000.0},
                             prices={"A": 110.0})  # B has no quote
    usd = out["by_currency"][0]
    assert usd["committed"] == 1500.0
    # A marked to 1100, B falls back to cost 500.
    assert usd["market_value"] == 1600.0
    assert out["total_market_value"] == 1600.0
    # Existing fields unchanged (backward compatibility).
    assert usd["available"] == 8500.0
    assert usd["exposure_pct"] == 0.15


def test_build_risk_report_additive_fields(tmp_data_dir: Path):
    (tmp_data_dir / "open_positions.json").write_text(json.dumps({
        "NVDA": {"symbol": "NVDA", "entry_price": 100.0, "stop_price": 95.0,
                 "quantity": 10, "currency": "USD"},
    }))
    with open(tmp_data_dir / "trades.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SCHEMA_COLUMNS)
        w.writeheader()

    # With quotes → marked to market.
    report = build_risk_report(tmp_data_dir, {"USD": 10_000.0}, 10_000.0,
                               ohlcv_fetcher=lambda s: None,
                               prices={"NVDA": 104.0}, daily_loss_limit_pct=0.015)
    d = report.to_dict()
    assert d["marked_to_market"] is True
    assert d["open_risk"]["total"] == 90.0
    assert d["daily_loss_budget"]["limit_usd"] == 150.0
    # Legacy consumers: original keys all still present.
    for key in ("exposure", "sector_concentration", "correlations",
                "max_correlation", "drawdown", "pnl_breakdown"):
        assert key in d

    # Without quotes → still renders, valued at cost, badge flipped.
    report = build_risk_report(tmp_data_dir, {"USD": 10_000.0}, 10_000.0,
                               ohlcv_fetcher=lambda s: None)
    d = report.to_dict()
    assert d["marked_to_market"] is False
    assert d["open_risk"]["total"] == 50.0
