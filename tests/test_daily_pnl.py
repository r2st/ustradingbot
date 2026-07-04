"""Tests for the daily P&L reset + persistence fix (P0).

Covers: same-day persistence across restarts, automatic day-boundary rollover,
stale prior-day state being discarded on load, and the daily-loss limit gate
reading only *today's* accumulated P&L.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from config.settings import Settings
from risk.manager import RiskManager
from signals.signal_types import Grade, Signal


def _valid_signal() -> Signal:
    return Signal(
        symbol="AAPL",
        strategy="momentum",
        entry_price=100.0,
        stop_price=95.0,
        target_price=115.0,
        signal_strength=0.82,
        grade=Grade.A,
        timestamp=datetime.now(),
    )


def _write_pnl_file(data_dir: Path, date: str, pnl: float) -> None:
    (data_dir / "daily_pnl.json").write_text(
        json.dumps({"date": date, "pnl": pnl}), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #


def test_record_persists_within_same_day(settings: Settings) -> None:
    rm = RiskManager(settings)
    rm.record_daily_pnl(-120.0)
    assert rm.daily_pnl == pytest.approx(-120.0)

    # A fresh manager (simulated restart) reloads the same-day accumulator.
    rm2 = RiskManager(settings)
    assert rm2.daily_pnl == pytest.approx(-120.0)


def test_record_accumulates(settings: Settings) -> None:
    rm = RiskManager(settings)
    rm.record_daily_pnl(-50.0)
    rm.record_daily_pnl(-30.0)
    rm.record_daily_pnl(20.0)
    assert rm.daily_pnl == pytest.approx(-60.0)


def test_load_discards_stale_previous_day(settings: Settings, monkeypatch) -> None:
    # Persisted loss belongs to yesterday.
    _write_pnl_file(settings.DATA_DIR, "2026-07-03", -500.0)
    monkeypatch.setattr("risk.manager._trading_day", lambda: "2026-07-04")

    rm = RiskManager(settings)
    assert rm.daily_pnl == 0.0  # not carried forward


def test_load_restores_same_day(settings: Settings, monkeypatch) -> None:
    _write_pnl_file(settings.DATA_DIR, "2026-07-04", -300.0)
    monkeypatch.setattr("risk.manager._trading_day", lambda: "2026-07-04")

    rm = RiskManager(settings)
    assert rm.daily_pnl == pytest.approx(-300.0)


def test_load_corrupt_file_resets(settings: Settings) -> None:
    (settings.DATA_DIR / "daily_pnl.json").write_text("{not json", encoding="utf-8")
    rm = RiskManager(settings)
    assert rm.daily_pnl == 0.0


# --------------------------------------------------------------------------- #
# rollover
# --------------------------------------------------------------------------- #


def test_maybe_reset_rolls_over_on_new_day(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr("risk.manager._trading_day", lambda: "2026-07-04")
    rm = RiskManager(settings)
    rm.record_daily_pnl(-200.0)
    assert rm.daily_pnl == pytest.approx(-200.0)

    # Advance to the next trading day.
    monkeypatch.setattr("risk.manager._trading_day", lambda: "2026-07-05")
    assert rm.maybe_reset_daily_pnl() is True
    assert rm.daily_pnl == 0.0

    # Persisted state now reflects the new day at zero.
    saved = json.loads((settings.DATA_DIR / "daily_pnl.json").read_text())
    assert saved["date"] == "2026-07-05"
    assert saved["pnl"] == 0.0


def test_maybe_reset_noop_same_day(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr("risk.manager._trading_day", lambda: "2026-07-04")
    rm = RiskManager(settings)
    rm.record_daily_pnl(-75.0)
    assert rm.maybe_reset_daily_pnl() is False
    assert rm.daily_pnl == pytest.approx(-75.0)


def test_record_auto_rolls_over(settings: Settings, monkeypatch) -> None:
    """record_daily_pnl on a new day starts fresh rather than accumulating."""
    monkeypatch.setattr("risk.manager._trading_day", lambda: "2026-07-04")
    rm = RiskManager(settings)
    rm.record_daily_pnl(-200.0)

    monkeypatch.setattr("risk.manager._trading_day", lambda: "2026-07-05")
    rm.record_daily_pnl(-40.0)
    assert rm.daily_pnl == pytest.approx(-40.0)


def test_reset_daily_pnl_zeroes_for_today(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr("risk.manager._trading_day", lambda: "2026-07-04")
    rm = RiskManager(settings)
    rm.record_daily_pnl(-90.0)
    rm.reset_daily_pnl()
    assert rm.daily_pnl == 0.0
    saved = json.loads((settings.DATA_DIR / "daily_pnl.json").read_text())
    assert saved["pnl"] == 0.0
    assert saved["date"] == "2026-07-04"


# --------------------------------------------------------------------------- #
# daily-loss limit gate uses today's P&L only
# --------------------------------------------------------------------------- #


def test_daily_loss_limit_blocks_when_hit_today(settings: Settings) -> None:
    rm = RiskManager(settings)
    limit = settings.TOTAL_CAPITAL * settings.DAILY_LOSS_LIMIT_PCT
    rm.record_daily_pnl(-(limit + 1.0))  # breach the limit
    ok, reason = rm.pre_check(_valid_signal())
    assert ok is False
    assert "daily_loss_limit_reached" in reason


def test_prior_day_loss_does_not_block_today(settings: Settings, monkeypatch) -> None:
    """The core bug: yesterday's loss must not block today's entries."""
    # A huge loss persisted for the previous trading day.
    _write_pnl_file(settings.DATA_DIR, "2026-07-03", -9_999.0)
    monkeypatch.setattr("risk.manager._trading_day", lambda: "2026-07-04")

    rm = RiskManager(settings)
    assert rm.daily_pnl == 0.0
    ok, reason = rm.pre_check(_valid_signal())
    assert ok is True, reason
