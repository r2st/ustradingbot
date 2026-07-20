"""Tests for custom indicator & portfolio alerts (P1-4)."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from alerts.indicator_alerts import (
    IndicatorAlertError,
    IndicatorAlertStore,
    check_indicator_alerts,
    evaluate_ma_cross,
    evaluate_rsi_cross,
    evaluate_threshold,
    evaluate_volume_spike,
)
from config.settings import Settings


# ---------------------------------------------------------------------------
# Pure evaluators
# ---------------------------------------------------------------------------


class TestEvaluators:
    def test_rsi_cross_above(self) -> None:
        rsi = pd.Series([50, 55, 68, 72])  # crosses above 70 on last bar
        assert evaluate_rsi_cross(rsi, "above", 70) == 72
        # No cross if already above both bars.
        assert evaluate_rsi_cross(pd.Series([72, 75]), "above", 70) is None

    def test_rsi_cross_below(self) -> None:
        rsi = pd.Series([40, 35, 28])
        assert evaluate_rsi_cross(rsi, "below", 30) == 28
        assert evaluate_rsi_cross(pd.Series([25, 20]), "below", 30) is None

    def test_ma_golden_cross(self) -> None:
        # Flat then a sharp final jump: the 2-bar MA crosses above the 4-bar MA
        # exactly on the last bar.
        close = pd.Series([100.0] * 10 + [130.0])
        out = evaluate_ma_cross(close, fast=2, slow=4, kind="golden")
        assert out is not None
        assert out["fast_ma"] > out["slow_ma"]
        # A death-cross rule does not fire on the same upward cross.
        assert evaluate_ma_cross(close, fast=2, slow=4, kind="death") is None

    def test_ma_cross_requires_fast_lt_slow(self) -> None:
        with pytest.raises(IndicatorAlertError):
            evaluate_ma_cross(pd.Series([1, 2, 3]), fast=20, slow=5, kind="golden")

    def test_volume_spike(self) -> None:
        vol = pd.Series([1_000_000] * 20 + [1_600_000])  # 60% above avg
        out = evaluate_volume_spike(vol, pct_above_avg=50)
        assert out is not None and out["pct_above"] == pytest.approx(60.0, abs=1)
        # Below threshold -> None.
        assert evaluate_volume_spike(pd.Series([1_000_000] * 20 + [1_100_000]), 50) is None

    def test_threshold(self) -> None:
        assert evaluate_threshold(12.0, 10.0) == 12.0
        assert evaluate_threshold(8.0, 10.0) is None
        assert evaluate_threshold(None, 10.0) is None


# ---------------------------------------------------------------------------
# Store validation
# ---------------------------------------------------------------------------


class TestStore:
    def test_add_rsi_rule(self, tmp_path) -> None:
        store = IndicatorAlertStore(tmp_path)
        rule = store.add_alert(
            "rsi_cross", {"symbol": "AAPL", "direction": "above", "threshold": 70}
        )
        assert rule["type"] == "rsi_cross"
        assert rule["symbol"] == "AAPL"
        assert store.list_alerts()[0]["id"] == rule["id"]

    def test_add_daily_loss_rule(self, tmp_path) -> None:
        store = IndicatorAlertStore(tmp_path)
        rule = store.add_alert("daily_loss", {"threshold_pct": 1.5})
        assert rule["threshold_pct"] == 1.5
        assert "symbol" not in rule

    def test_invalid_type_rejected(self, tmp_path) -> None:
        with pytest.raises(IndicatorAlertError):
            IndicatorAlertStore(tmp_path).add_alert("bogus", {})

    def test_ma_cross_validates_periods(self, tmp_path) -> None:
        with pytest.raises(IndicatorAlertError):
            IndicatorAlertStore(tmp_path).add_alert(
                "ma_cross", {"symbol": "AAPL", "fast": 200, "slow": 50}
            )

    def test_delete_and_set_active(self, tmp_path) -> None:
        store = IndicatorAlertStore(tmp_path)
        rule = store.add_alert("daily_loss", {"threshold_pct": 1.0})
        assert store.set_active(rule["id"], False)["active"] is False
        assert store.delete_alert(rule["id"]) is True
        assert store.delete_alert(rule["id"]) is False


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------


def _volume_spike_df() -> pd.DataFrame:
    # Flat 1M volume for 24 bars, then a 60% spike on the final bar.
    n = 25
    close = np.full(n, 100.0)
    vol = [1_000_000.0] * (n - 1) + [1_600_000.0]
    idx = pd.bdate_range(end=datetime(2024, 6, 1), periods=n)
    return pd.DataFrame(
        {"Open": close, "High": close * 1.01, "Low": close * 0.99,
         "Close": close, "Volume": vol},
        index=idx,
    )


class TestChecker:
    def test_daily_loss_fires(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("dashboard.push.publish", lambda *a, **k: None)
        settings = Settings(DATA_DIR=tmp_path)
        store = IndicatorAlertStore(tmp_path)
        store.add_alert("daily_loss", {"threshold_pct": 1.0})
        fired = check_indicator_alerts(
            settings, ohlcv_fetcher=lambda s: None,
            portfolio_ctx={"daily_loss_pct": 2.0},
        )
        assert len(fired) == 1
        # Marked triggered -> does not re-fire.
        again = check_indicator_alerts(
            settings, ohlcv_fetcher=lambda s: None,
            portfolio_ctx={"daily_loss_pct": 2.0},
        )
        assert again == []

    def test_daily_loss_below_threshold_no_fire(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("dashboard.push.publish", lambda *a, **k: None)
        settings = Settings(DATA_DIR=tmp_path)
        IndicatorAlertStore(tmp_path).add_alert("daily_loss", {"threshold_pct": 5.0})
        fired = check_indicator_alerts(
            settings, ohlcv_fetcher=lambda s: None,
            portfolio_ctx={"daily_loss_pct": 2.0},
        )
        assert fired == []

    def test_volume_spike_fires_from_ohlcv(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("dashboard.push.publish", lambda *a, **k: None)
        settings = Settings(DATA_DIR=tmp_path)
        IndicatorAlertStore(tmp_path).add_alert(
            "volume_spike", {"symbol": "AAPL", "pct_above_avg": 50}
        )
        df = _volume_spike_df()
        fired = check_indicator_alerts(settings, ohlcv_fetcher=lambda s: df)
        assert len(fired) == 1
        assert fired[0]["type"] == "volume_spike"

    def test_no_armed_rules_returns_empty(self, tmp_path) -> None:
        settings = Settings(DATA_DIR=tmp_path)
        assert check_indicator_alerts(settings, ohlcv_fetcher=lambda s: None) == []
