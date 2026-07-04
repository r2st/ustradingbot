"""Tests for the Monte Carlo projection (feature 8)."""

from __future__ import annotations

from pathlib import Path

from analytics.montecarlo import run_from_journal, simulate
from config.settings import Settings
from journal.trade_logger import SCHEMA_COLUMNS


def test_empty_samples_zeroed():
    r = simulate([], starting_capital=10_000, runs=100, horizon=10)
    assert r.trades_sampled == 0
    assert r.mean_ending == 10_000
    assert r.percentiles["p50"] == 10_000


def test_all_positive_grows_and_profits():
    r = simulate([50.0] * 20, starting_capital=10_000, runs=500, horizon=20)
    assert r.mean_ending > 10_000
    assert r.prob_profit > 0.99
    assert r.prob_loss_10pct == 0.0


def test_all_negative_loses():
    r = simulate([-100.0] * 20, starting_capital=10_000, runs=500, horizon=20)
    assert r.prob_profit < 0.01
    assert r.prob_loss_10pct > 0.9


def test_percentiles_ordered():
    r = simulate([10, -5, 20, -15, 8], starting_capital=5_000, runs=800, horizon=30)
    p = r.percentiles
    assert p["p5"] <= p["p25"] <= p["p50"] <= p["p75"] <= p["p95"]


def test_determinism():
    a = simulate([10, -5, 20], 5_000, runs=300, horizon=25, seed=7)
    b = simulate([10, -5, 20], 5_000, runs=300, horizon=25, seed=7)
    assert a.mean_ending == b.mean_ending
    assert a.percentiles == b.percentiles


def test_sample_paths_shape():
    r = simulate([10, -5], 1_000, runs=50, horizon=12, n_sample_paths=20)
    assert len(r.sample_paths) == 20
    assert all(len(path) == 13 for path in r.sample_paths)  # horizon + 1


def test_run_from_journal(tmp_data_dir: Path):
    csv = tmp_data_dir / "trades.csv"
    header = ",".join(SCHEMA_COLUMNS)
    rows = [header]
    for i, pnl in enumerate([120.0, -40.0, 80.0], start=1):
        row = {c: "" for c in SCHEMA_COLUMNS}
        row.update({
            "trade_id": str(i), "symbol": "AAPL", "strategy": "momentum",
            "entry_fill_price": "100", "quantity": "10", "exit_price": "110",
            "exit_time": f"2026-06-0{i}T15:00:00", "pnl_net": str(pnl),
        })
        rows.append(",".join(row[c] for c in SCHEMA_COLUMNS))
    csv.write_text("\n".join(rows) + "\n", encoding="utf-8")

    settings = Settings(DATA_DIR=tmp_data_dir, TOTAL_CAPITAL=10_000,
                        MONTE_CARLO_RUNS=200, MONTE_CARLO_HORIZON=10)
    r = run_from_journal(settings)
    assert r.trades_sampled == 3
    assert r.runs == 200
