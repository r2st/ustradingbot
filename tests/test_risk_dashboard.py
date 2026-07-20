"""Tests for the portfolio risk dashboard (analytics/risk_dashboard.py)."""

from __future__ import annotations

import json
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from analytics.risk_dashboard import (
    build_risk_report,
    drawdown_tracking,
    pnl_breakdown,
    portfolio_exposure,
    position_correlations,
    returns_from_ohlcv,
    sector_concentration,
)
from config.settings import Settings
from journal.trade_logger import TradeLogger
from signals.signal_types import ExitEvent, ExitReason, Signal, TradeOrder


# --------------------------------------------------------------------------- #
# exposure
# --------------------------------------------------------------------------- #


def test_portfolio_exposure() -> None:
    positions = [
        {"symbol": "AAPL", "entry_price": 100.0, "quantity": 10, "currency": "USD"},
        {"symbol": "MSFT", "entry_price": 200.0, "quantity": 5, "currency": "USD"},
        {"symbol": "SHOP.TO", "entry_price": 50.0, "quantity": 10, "currency": "CAD"},
    ]
    exp = portfolio_exposure(positions, {"USD": 9000.0, "CAD": 3000.0})
    usd = next(r for r in exp["by_currency"] if r["currency"] == "USD")
    assert usd["committed"] == pytest.approx(2000.0)  # 1000 + 1000
    assert usd["available"] == pytest.approx(7000.0)
    cad = next(r for r in exp["by_currency"] if r["currency"] == "CAD")
    assert cad["committed"] == pytest.approx(500.0)
    assert exp["total_committed"] == pytest.approx(2500.0)
    assert exp["open_positions"] == 3


def test_exposure_empty() -> None:
    exp = portfolio_exposure([], {"USD": 9000.0})
    assert exp["total_committed"] == 0.0
    assert exp["gross_exposure_pct"] == 0.0


# --------------------------------------------------------------------------- #
# sector concentration
# --------------------------------------------------------------------------- #


def test_sector_concentration() -> None:
    positions = [
        {"symbol": "AAPL", "entry_price": 100.0, "quantity": 10},  # Technology 1000
        {"symbol": "MSFT", "entry_price": 100.0, "quantity": 10},  # Technology 1000
        {"symbol": "JPM", "entry_price": 100.0, "quantity": 5},    # Financials 500
    ]
    rows = sector_concentration(positions)
    tech = next(r for r in rows if r["sector"] == "Technology")
    assert tech["positions"] == 2
    assert tech["exposure"] == pytest.approx(2000.0)
    assert tech["pct"] == pytest.approx(2000.0 / 2500.0, abs=0.001)
    # Sorted largest first.
    assert rows[0]["sector"] == "Technology"


def test_sector_unknown_symbol() -> None:
    rows = sector_concentration([{"symbol": "ZZZZ", "entry_price": 10.0, "quantity": 1}])
    assert rows[0]["sector"] == "Unknown"


# --------------------------------------------------------------------------- #
# correlations
# --------------------------------------------------------------------------- #


def test_position_correlations_perfectly_correlated() -> None:
    base = pd.Series(np.linspace(0.01, 0.05, 30))
    returns = {"AAA": base, "BBB": base * 1.0, "CCC": -base}
    corr = position_correlations(returns, min_overlap=10)
    pair = {(c["a"], c["b"]): c["correlation"] for c in corr}
    assert pair[("AAA", "BBB")] == pytest.approx(1.0, abs=0.001)
    assert pair[("AAA", "CCC")] == pytest.approx(-1.0, abs=0.001)
    # Sorted by |correlation| desc — all are 1.0 here.
    assert abs(corr[0]["correlation"]) == pytest.approx(1.0)


def test_position_correlations_insufficient_overlap() -> None:
    returns = {"AAA": pd.Series([0.01, 0.02]), "BBB": pd.Series([0.01, 0.02])}
    assert position_correlations(returns, min_overlap=20) == []


def test_returns_from_ohlcv() -> None:
    df = pd.DataFrame({"Close": [100.0, 101.0, 102.0, 101.0]})
    r = returns_from_ohlcv(df)
    assert r is not None
    assert len(r) == 3


def test_returns_from_ohlcv_too_short() -> None:
    assert returns_from_ohlcv(pd.DataFrame({"Close": [100.0]})) is None


# --------------------------------------------------------------------------- #
# drawdown
# --------------------------------------------------------------------------- #


def _trades_df(pnls):
    rows = []
    for i, p in enumerate(pnls):
        rows.append({
            "pnl_net": p, "r_multiple": 1.0, "strategy": "momentum",
            "symbol": "AAPL", "exit_time": f"2026-07-{i+1:02d}T16:00:00",
        })
    return pd.DataFrame(rows)


def test_drawdown_tracking() -> None:
    # +100, -300, +50 -> equity 10000, 10100, 9800, 9850; peak 10100
    trades = _trades_df([100.0, -300.0, 50.0])
    dd = drawdown_tracking(trades, 10000.0)
    assert dd["peak_equity"] == pytest.approx(10100.0)
    assert dd["current_equity"] == pytest.approx(9850.0)
    assert dd["current_drawdown_abs"] == pytest.approx(250.0)
    assert dd["max_drawdown_abs"] == pytest.approx(300.0)  # 10100 -> 9800


def test_drawdown_empty() -> None:
    dd = drawdown_tracking(pd.DataFrame(), 10000.0)
    assert dd["current_drawdown_pct"] == 0.0
    assert dd["max_drawdown_abs"] == 0.0


# --------------------------------------------------------------------------- #
# pnl breakdown
# --------------------------------------------------------------------------- #


def test_pnl_breakdown_periods() -> None:
    now = datetime(2026, 7, 15, 12, 0, 0)  # a Wednesday
    trades = pd.DataFrame([
        {"pnl_net": 100.0, "exit_time": "2026-07-15T10:00:00"},  # today
        {"pnl_net": 50.0, "exit_time": "2026-07-14T10:00:00"},   # this week (Mon)
        {"pnl_net": 25.0, "exit_time": "2026-07-02T10:00:00"},   # this month
        {"pnl_net": -10.0, "exit_time": "2026-06-20T10:00:00"},  # prior month
    ])
    b = pnl_breakdown(trades, now=now)
    assert b["today"] == pytest.approx(100.0)
    assert b["week"] == pytest.approx(150.0)   # 100 + 50
    assert b["month"] == pytest.approx(175.0)  # 100 + 50 + 25
    assert len(b["daily"]) >= 1
    assert len(b["monthly"]) >= 1


def test_pnl_breakdown_empty() -> None:
    b = pnl_breakdown(pd.DataFrame())
    assert b["today"] == 0.0
    assert b["daily"] == []


# --------------------------------------------------------------------------- #
# build_risk_report (integration)
# --------------------------------------------------------------------------- #


def test_build_risk_report(settings: Settings) -> None:
    data_dir = settings.DATA_DIR
    (data_dir / "open_positions.json").write_text(json.dumps({
        "AAPL": {"symbol": "AAPL", "entry_price": 100.0, "quantity": 10,
                 "currency": "USD"},
        "MSFT": {"symbol": "MSFT", "entry_price": 200.0, "quantity": 5,
                 "currency": "USD"},
    }))
    journal = TradeLogger(str(data_dir))
    sig = Signal(symbol="AAPL", strategy="momentum", entry_price=100.0,
                 stop_price=95.0, target_price=115.0)
    order = TradeOrder(signal=sig, quantity=10, currency="USD")
    journal.log_entry(order, 100.0)
    journal.log_exit("AAPL", ExitEvent(symbol="AAPL", exit_price=115.0,
                                       exit_reason=ExitReason.TARGET_HIT))

    # Deterministic fake OHLCV fetcher for correlation.
    def fake_fetch(symbol):
        rng = np.random.default_rng(hash(symbol) % 1000)
        closes = 100 + np.cumsum(rng.normal(0, 1, 80))
        return pd.DataFrame({"Close": closes})

    report = build_risk_report(
        data_dir, {"USD": 9000.0, "CAD": 3000.0}, 12000.0,
        ohlcv_fetcher=fake_fetch,
    )
    assert report.exposure["open_positions"] == 2
    assert any(s["sector"] == "Technology" for s in report.sector_concentration)
    assert len(report.correlations) == 1  # AAPL/MSFT pair
    assert "current_drawdown_pct" in report.drawdown
    assert "today" in report.pnl_breakdown
    # to_dict round-trips (monitoring F5 added open_risk / daily_loss_budget /
    # marked_to_market; P3f added beta — additions only, original keys survive).
    assert set(report.to_dict()) == {
        "exposure", "sector_concentration", "correlations",
        "max_correlation", "drawdown", "pnl_breakdown",
        "open_risk", "daily_loss_budget", "marked_to_market", "beta",
    }


def test_build_risk_report_no_positions(settings: Settings) -> None:
    report = build_risk_report(settings.DATA_DIR, {"USD": 9000.0}, 9000.0,
                               ohlcv_fetcher=lambda s: None)
    assert report.exposure["open_positions"] == 0
    assert report.correlations == []
    assert report.max_correlation is None
