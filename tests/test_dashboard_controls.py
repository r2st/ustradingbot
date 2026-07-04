"""Tests for the engine-control and backtesting dashboard modules + routes."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
import dashboard.backtest_control as bc
import dashboard.engine_control as ec
from config.settings import Settings


@pytest.fixture
def client() -> TestClient:
    return TestClient(dash.app, raise_server_exceptions=False)


def _use(monkeypatch, data_dir, **kw) -> Settings:
    settings = Settings(DASHBOARD_AUTH_ENABLED=False, DATA_DIR=data_dir, **kw)
    monkeypatch.setattr(dash, "get_settings", lambda: settings)
    return settings


# ----------------------------------------------------------------- heartbeat


def test_heartbeat_round_trip(tmp_path):
    ec.write_heartbeat(tmp_path, phase="scanning", open_positions=3)
    hb = ec.read_heartbeat(tmp_path)
    assert hb["phase"] == "scanning"
    assert hb["open_positions"] == 3
    assert "updated_at" in hb  # stamped automatically


def test_read_heartbeat_missing(tmp_path):
    assert ec.read_heartbeat(tmp_path) == {}


def test_engine_status_shape(tmp_path):
    settings = Settings(DATA_DIR=tmp_path)
    status = ec.engine_status(settings)
    assert set(status) >= {"state", "controllable", "service", "activity"}
    # Off the production host the unit is not managed, so not controllable.
    assert status["controllable"] is False
    assert status["state"] in {"running", "stopped", "error", "unknown"}


def test_fresh_heartbeat_reads_back(tmp_path):
    settings = Settings(DATA_DIR=tmp_path, SCAN_INTERVAL_MINUTES=60)
    ec.write_heartbeat(tmp_path, phase="waiting", open_positions=0)
    status = ec.engine_status(settings)
    assert status["activity"]["phase"] == "waiting"
    assert status["activity"]["stale"] is False


# ------------------------------------------------------------- engine control


def test_control_requires_admin_password(tmp_path):
    settings = Settings(DATA_DIR=tmp_path, DASHBOARD_PASSWORD="")
    result = ec.control_engine("restart", "", settings)
    assert result["ok"] is False
    assert "no admin password" in result["message"].lower()


def test_control_rejects_wrong_password(tmp_path):
    settings = Settings(DATA_DIR=tmp_path, DASHBOARD_PASSWORD="s3cret")
    result = ec.control_engine("restart", "wrong", settings)
    assert result["ok"] is False
    assert "invalid admin password" in result["message"].lower()


def test_control_rejects_unknown_action(tmp_path):
    settings = Settings(DATA_DIR=tmp_path, DASHBOARD_PASSWORD="s3cret")
    result = ec.control_engine("nuke", "s3cret", settings)
    assert result["ok"] is False
    assert "unknown action" in result["message"].lower()


def test_engine_logs_shape():
    logs = ec.engine_logs(50)
    assert "available" in logs and "lines" in logs
    assert isinstance(logs["lines"], list)


def test_format_log_line_prettifies_json():
    raw = ('{"event": "engine.sleeping", "level": "info", '
           '"timestamp": "2026-07-04T18:03:00.621585Z", "sleep_minutes": 60}')
    out = ec._format_log_line(raw)
    assert "engine.sleeping" in out
    assert "sleep_minutes=60" in out
    # Non-JSON lines pass through untouched.
    assert ec._format_log_line("plain text") == "plain text"


# ---------------------------------------------------------------- backtest cfg


def test_options_has_defaults_and_pead_off():
    opts = bc.options()
    assert opts["symbols"] and opts["strategies"] and opts["grades"]
    pead = next(s for s in opts["strategies"] if s["value"] == "pead")
    assert pead["default"] is False
    assert all(
        s["default"] for s in opts["strategies"] if s["value"] != "pead"
    )


@pytest.mark.parametrize(
    "params, needle",
    [
        ({"symbols": [], "strategies": ["momentum"],
          "start": "2024-01-01", "end": "2024-06-01"}, "at least one symbol"),
        ({"symbols": ["AAPL"], "strategies": [],
          "start": "2024-01-01", "end": "2024-06-01"}, "at least one strategy"),
        ({"symbols": ["AAPL"], "strategies": ["momentum"],
          "start": "2024-06-01", "end": "2024-01-01"}, "before the end"),
        ({"symbols": [f"S{i}" for i in range(41)], "strategies": ["momentum"],
          "start": "2024-01-01", "end": "2024-06-01"}, "too many symbols"),
        ({"symbols": ["AAPL"], "strategies": ["momentum"],
          "start": "", "end": "2024-06-01"}, "required"),
    ],
)
def test_build_config_validation(params, needle):
    with pytest.raises(ValueError) as exc:
        bc._build_config(params)
    assert needle in str(exc.value).lower()


def test_build_config_normalises_symbols():
    cfg, meta = bc._build_config({
        "symbols": "aapl, aapl msft",  # dupes + lowercase + mixed separators
        "strategies": ["momentum", "bogus"],  # unknown strategy dropped
        "start": "2024-01-01", "end": "2024-06-01",
    })
    assert cfg.symbols == ["AAPL", "MSFT"]
    assert cfg.strategies == ["momentum"]
    assert meta["start"] == "2024-01-01"


def test_start_backtest_runs_in_background(monkeypatch):
    """The job registry drives a run to completion off a background thread."""

    class _FakeResult:
        summary = {"total_trades": 2, "total_pnl": 12.5, "win_rate": 0.5}

        def to_dict(self):
            return {"summary": self.summary, "equity_curve": [],
                    "trades": [], "by_strategy": [], "by_symbol": [], "config": {}}

    monkeypatch.setattr("backtest.engine.run_backtest", lambda cfg: _FakeResult())

    started = bc.start_backtest({
        "symbols": ["AAPL"], "strategies": ["momentum"],
        "start": "2024-01-01", "end": "2024-06-01",
    })
    assert started["ok"] is True
    job_id = started["job_id"]

    for _ in range(50):
        job = bc.get_job(job_id)
        if job and job["state"] != "running":
            break
        time.sleep(0.05)

    job = bc.get_job(job_id)
    assert job["state"] == "done"
    assert job["result"]["summary"]["total_trades"] == 2
    assert job["result"]["trade_count"] == 0


def test_start_backtest_rejects_bad_params():
    result = bc.start_backtest({"symbols": [], "strategies": []})
    assert result["ok"] is False
    assert "job_id" not in result


# --------------------------------------------------------------------- routes


def test_engine_status_route(client, monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path)
    resp = client.get("/api/engine/status")
    assert resp.status_code == 200
    assert "state" in resp.json()


def test_engine_control_route_bad_password(client, monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path, DASHBOARD_PASSWORD="s3cret")
    resp = client.post("/api/engine/control",
                       json={"action": "restart", "admin_password": "nope"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_backtest_options_route(client, monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path)
    resp = client.get("/api/backtest/options")
    assert resp.status_code == 200
    assert "symbols" in resp.json()


def test_backtest_status_route_unknown_job(client, monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path)
    resp = client.get("/api/backtest/status/does-not-exist")
    assert resp.status_code == 404


def test_backtest_run_route_validates(client, monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path)
    resp = client.post("/api/backtest/run", json={"symbols": [], "strategies": []})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
