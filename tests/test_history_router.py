"""Tests for the trade history & analytics API (monitoring F2)."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
from config.settings import Settings
from journal.trade_logger import SCHEMA_COLUMNS


@pytest.fixture
def client() -> TestClient:
    return TestClient(dash.app, raise_server_exceptions=False)


@pytest.fixture
def env(monkeypatch, tmp_path):
    data_dir = tmp_path / "data_store"
    data_dir.mkdir()
    settings = Settings(DASHBOARD_AUTH_ENABLED=False, DATA_DIR=data_dir)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    return data_dir


def _row(**kw) -> dict:
    row = {c: "" for c in SCHEMA_COLUMNS}
    row.update(kw)
    return row


def _write_journal(data_dir: Path, rows) -> None:
    with open(data_dir / "trades.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SCHEMA_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def _seed(data_dir: Path) -> None:
    rows = []
    for i in range(30):
        pnl = 10.0 if i % 3 else -5.0  # 20 wins, 10 losses
        rows.append(_row(
            trade_id=i + 1, symbol=f"SYM{i % 5}",
            strategy="momentum" if i % 2 else "swing",
            direction="long", quantity=10, entry_fill_price=100 + i,
            entry_time=f"2026-06-{(i % 28) + 1:02d}T10:00:00",
            exit_time=f"2026-06-{(i % 28) + 1:02d}T15:00:00",
            exit_price=101 + i, exit_reason="TARGET_HIT" if pnl > 0 else "STOP_HIT",
            pnl_net=pnl, pnl_pct=pnl / 10, r_multiple=pnl / 10,
            hold_duration_hours=5.0, grade="A",
        ))
    # One still-open row that must be excluded everywhere.
    rows.append(_row(trade_id=99, symbol="OPEN", strategy="momentum",
                     entry_fill_price=50, entry_time="2026-06-30T10:00:00"))
    _write_journal(data_dir, rows)


def test_pagination_and_total(client, env):
    _seed(env)
    d = client.get("/api/history/trades?limit=10&offset=0").json()
    assert d["total"] == 30  # open row excluded
    assert len(d["trades"]) == 10
    d2 = client.get("/api/history/trades?limit=10&offset=25").json()
    assert len(d2["trades"]) == 5
    # No page ever contains the open row.
    assert all(t["symbol"] != "OPEN" for t in d["trades"] + d2["trades"])


def test_filters_combine(client, env):
    _seed(env)
    d = client.get(
        "/api/history/trades?strategy=momentum&symbol=SYM1&limit=100"
    ).json()
    assert d["total"] > 0
    assert all(t["strategy"] == "momentum" and t["symbol"] == "SYM1"
               for t in d["trades"])
    d = client.get(
        "/api/history/trades?exit_reason=STOP_HIT&date_from=2026-06-01&date_to=2026-06-30&limit=100"
    ).json()
    assert d["total"] == 10
    assert all(t["exit_reason"] == "STOP_HIT" for t in d["trades"])


def test_sorting(client, env):
    _seed(env)
    d = client.get("/api/history/trades?sort=pnl_net&order=desc&limit=5").json()
    pnls = [t["pnl_net"] for t in d["trades"]]
    assert pnls == sorted(pnls, reverse=True)


def test_stats(client, env):
    _seed(env)
    s = client.get("/api/history/stats").json()
    assert s["avg_hold_hours"] == 5.0
    assert s["best"]["pnl_net"] == 10.0
    assert s["worst"]["pnl_net"] == -5.0
    assert s["by_exit_reason"]["STOP_HIT"]["count"] == 10
    assert s["longest_win_streak"] >= 2


def test_win_rate_trend_hand_check(client, env):
    # 4 trades: W W L W → rolling(2) win rates: 1.0, 1.0, 0.5, 0.5
    rows = []
    for i, pnl in enumerate([5.0, 5.0, -5.0, 5.0]):
        rows.append(_row(
            trade_id=i + 1, symbol="X", strategy="swing", quantity=1,
            entry_fill_price=10, entry_time=f"2026-06-0{i + 1}T10:00:00",
            exit_time=f"2026-06-0{i + 1}T15:00:00", exit_price=11,
            exit_reason="MANUAL", pnl_net=pnl, r_multiple=pnl,
        ))
    _write_journal(env, rows)
    d = client.get("/api/history/win-rate-trend?window=2").json()
    rates = [p["win_rate"] for p in d["points"]]
    assert rates == [1.0, 1.0, 0.5, 0.5]


def test_empty_journal(client, env):
    assert client.get("/api/history/trades").json() == {"total": 0, "trades": []}
    s = client.get("/api/history/stats").json()
    assert s["best"] is None and s["worst"] is None
    assert client.get("/api/history/win-rate-trend").json()["points"] == []


def test_malformed_numeric_cells_do_not_crash(client, env):
    rows = [_row(
        trade_id=1, symbol="BAD", strategy="swing", quantity="oops",
        entry_fill_price="not-a-number", entry_time="2026-06-01T10:00:00",
        exit_time="2026-06-01T15:00:00", exit_price="", exit_reason="MANUAL",
        pnl_net="garbage", r_multiple="", hold_duration_hours="",
    )]
    _write_journal(env, rows)
    d = client.get("/api/history/trades").json()
    assert d["total"] == 1
    assert d["trades"][0]["pnl_net"] is None  # None, never NaN
    assert client.get("/api/history/stats").status_code == 200
