"""P3f — portfolio beta vs SPY + market-relative drawdown."""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.risk_dashboard import (
    build_risk_report,
    market_relative_drawdown,
    portfolio_beta,
)


def _rng():
    # Deterministic pseudo-random market returns (no Math.random equivalent).
    return np.sin(np.linspace(0, 20, 120)) * 0.01 + 0.001


def test_beta_one_when_asset_equals_market():
    idx = pd.date_range("2026-01-01", periods=120, freq="D")
    market = pd.Series(_rng(), index=idx)
    out = portfolio_beta({"AAA": market.copy()}, {"AAA": 1000.0}, market)
    assert out["portfolio_beta"] == 1.0
    assert out["benchmark"] == "SPY"


def test_beta_two_when_asset_is_double_market():
    idx = pd.date_range("2026-01-01", periods=120, freq="D")
    market = pd.Series(_rng(), index=idx)
    out = portfolio_beta({"AAA": market * 2}, {"AAA": 1000.0}, market)
    assert out["portfolio_beta"] == 2.0


def test_weighted_portfolio_beta_across_two_symbols():
    idx = pd.date_range("2026-01-01", periods=120, freq="D")
    market = pd.Series(_rng(), index=idx)
    returns = {"AAA": market.copy(), "BBB": market * 3}
    # Equal weights → beta = (1 + 3) / 2 = 2.0
    out = portfolio_beta(returns, {"AAA": 500.0, "BBB": 500.0}, market)
    assert out["portfolio_beta"] == 2.0


def test_insufficient_overlap_excluded_and_renormalized():
    idx = pd.date_range("2026-01-01", periods=120, freq="D")
    market = pd.Series(_rng(), index=idx)
    short = pd.Series(_rng()[:5], index=idx[:5])  # too few overlaps
    returns = {"AAA": market.copy(), "BBB": short}
    out = portfolio_beta(returns, {"AAA": 500.0, "BBB": 500.0}, market,
                         min_overlap=20)
    betas = {r["symbol"]: r["beta"] for r in out["per_position"]}
    assert betas["BBB"] is None
    # AAA carries the whole covered weight → portfolio beta == AAA beta (1.0)
    assert out["portfolio_beta"] == 1.0


def test_zero_variance_market_returns_none():
    idx = pd.date_range("2026-01-01", periods=120, freq="D")
    flat = pd.Series(np.zeros(120), index=idx)
    out = portfolio_beta({"AAA": pd.Series(_rng(), index=idx)},
                         {"AAA": 1000.0}, flat)
    assert out["portfolio_beta"] is None


def test_empty_book_returns_none():
    out = portfolio_beta({}, {}, None)
    assert out["portfolio_beta"] is None
    assert out["per_position"] == []


def test_market_relative_drawdown():
    # Portfolio down 20% from peak, benchmark down 10%.
    port = [100, 120, 96]      # peak 120 → current 96 → 20% dd
    bench = [100, 110, 99]     # peak 110 → current 99 → 10% dd
    out = market_relative_drawdown(port, bench)
    assert out["portfolio_drawdown_pct"] == 0.2
    assert out["benchmark_drawdown_pct"] == 0.1
    assert out["excess_drawdown_pct"] == 0.1


def test_market_relative_drawdown_empty():
    out = market_relative_drawdown([], [])
    assert out["portfolio_drawdown_pct"] == 0.0
    assert out["excess_drawdown_pct"] == 0.0


def test_build_risk_report_includes_beta(tmp_path):
    import json

    positions = {
        "AAPL": {"symbol": "AAPL", "quantity": 10, "entry_price": 100,
                 "stop_price": 95, "currency": "USD"},
        "MSFT": {"symbol": "MSFT", "quantity": 5, "entry_price": 200,
                 "stop_price": 190, "currency": "USD"},
    }
    (tmp_path / "open_positions.json").write_text(json.dumps(positions))

    idx = pd.date_range("2026-01-01", periods=120, freq="D")
    market = pd.Series(_rng(), index=idx)

    def fake_fetcher(symbol):
        base = market.copy()
        mult = {"AAPL": 1.0, "MSFT": 2.0, "SPY": 1.0}.get(symbol, 1.0)
        rets = base * mult
        close = (1 + rets).cumprod() * 100.0
        return pd.DataFrame({"Close": close.values}, index=idx)

    report = build_risk_report(
        tmp_path, {"USD": 10000.0}, 10000.0, ohlcv_fetcher=fake_fetcher
    )
    d = report.to_dict()
    assert "beta" in d
    assert isinstance(d["beta"]["portfolio_beta"], float)
    # Existing keys still present (additive change).
    for key in ("exposure", "drawdown", "open_risk", "pnl_breakdown"):
        assert key in d
