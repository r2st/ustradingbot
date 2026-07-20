"""Tests for the dashboard walk-forward job control (P0-2)."""

from __future__ import annotations

import pytest

from dashboard import backtest_control as bc


class TestBuildWfConfig:
    def test_valid_params(self) -> None:
        cfg, meta = bc._build_wf_config(
            {
                "symbols": ["AAPL", "MSFT"],
                "strategies": ["momentum"],
                "start": "2022-01-01",
                "end": "2024-01-01",
                "train_months": 12,
                "test_months": 3,
                "objective": "total_return_pct",
                "param_grid": {"min_grade": ["B", "C"], "max_positions": [5, 10]},
            }
        )
        assert cfg.train_months == 12
        assert cfg.test_months == 3
        assert cfg.objective == "total_return_pct"
        assert cfg.param_grid["min_grade"] == ["B", "C"]
        assert cfg.param_grid["max_positions"] == [5, 10]
        assert meta["train_months"] == 12

    def test_grid_allowlist_filters_unknown(self) -> None:
        cfg, _ = bc._build_wf_config(
            {
                "symbols": ["AAPL"],
                "strategies": ["momentum"],
                "start": "2022-01-01",
                "end": "2024-01-01",
                "param_grid": {"EVIL_KEY": [1, 2], "min_grade": ["B"]},
            }
        )
        assert "EVIL_KEY" not in cfg.param_grid
        assert "min_grade" in cfg.param_grid

    def test_rejects_non_positive_months(self) -> None:
        with pytest.raises(ValueError):
            bc._build_wf_config(
                {
                    "symbols": ["AAPL"],
                    "strategies": ["momentum"],
                    "start": "2022-01-01",
                    "end": "2024-01-01",
                    "train_months": 0,
                }
            )


class TestStartWalkForward:
    def test_invalid_returns_error(self) -> None:
        out = bc.start_walk_forward({"symbols": [], "strategies": []})
        assert out["ok"] is False
        assert "message" in out

    def test_valid_returns_job_id(self) -> None:
        # Empty symbol data means the background thread finishes fast/harmlessly;
        # we only assert the job was accepted and registered.
        out = bc.start_walk_forward(
            {
                "symbols": ["AAPL"],
                "strategies": ["momentum"],
                "start": "2022-01-01",
                "end": "2023-06-01",
                "train_months": 9,
                "test_months": 3,
            }
        )
        assert out["ok"] is True
        job = bc.get_job(out["job_id"])
        assert job is not None
        assert job["kind"] == "walk_forward"
