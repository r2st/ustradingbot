"""Integration tests wiring MAE/MFE through the risk manager, journal, autotune."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from automation.autotune import tune
from config.settings import Settings
from journal.trade_logger import SCHEMA_COLUMNS, TradeLogger
from risk.manager import RiskManager
from signals.signal_types import (
    ExitEvent,
    ExitReason,
    Grade,
    Signal,
    TradeOrder,
)


def _order(symbol="AAPL", entry=100.0, stop=95.0, target=115.0) -> TradeOrder:
    signal = Signal(
        symbol=symbol,
        strategy="momentum",
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        signal_strength=0.82,
        grade=Grade.A,
        timestamp=datetime.now(),
    )
    return TradeOrder(signal=signal, quantity=10, risk_amount=50.0)


# ---------------------------------------------------------------------------
# RiskManager.record_excursions + exit-history snapshot
# ---------------------------------------------------------------------------


def test_record_excursions_updates_positions(settings: Settings) -> None:
    rm = RiskManager(settings)
    rm.register_position(_order(), fill_price=100.0)

    changed = rm.record_excursions({"AAPL": (102.0, 96.0)})
    assert changed == 1
    pos = rm.get_open_positions()["AAPL"]
    assert pos["mae_price"] == 96.0
    assert pos["mfe_price"] == 102.0

    # A milder bar doesn't widen -> reports zero changes.
    assert rm.record_excursions({"AAPL": (101.0, 99.0)}) == 0
    # Unknown symbols are ignored.
    assert rm.record_excursions({"ZZZZ": (1.0, 1.0)}) == 0


def test_record_excursions_persists_across_reload(settings: Settings) -> None:
    rm = RiskManager(settings)
    rm.register_position(_order(), fill_price=100.0)
    rm.record_excursions({"AAPL": (108.0, 93.0)})

    # A fresh manager reading the same DATA_DIR sees the tracked extremes.
    rm2 = RiskManager(settings)
    pos = rm2.get_open_positions()["AAPL"]
    assert pos["mae_price"] == 93.0
    assert pos["mfe_price"] == 108.0


def test_remove_position_records_excursion_in_history(settings: Settings) -> None:
    rm = RiskManager(settings)
    rm.register_position(_order(entry=100.0, stop=95.0), fill_price=100.0)
    rm.record_excursions({"AAPL": (110.0, 96.0)})

    event = ExitEvent(
        symbol="AAPL",
        exit_price=108.0,
        exit_reason=ExitReason.TARGET_HIT,
        exit_date=datetime.now(),
        pnl_gross=80.0,
    )
    rm.remove_position("AAPL", event)

    history = rm._exit_history  # noqa: SLF001 — asserting persisted record
    assert history[-1]["symbol"] == "AAPL"
    # entry 100, stop 95 -> risk 5. MAE to 96 (-4 => 0.8R), MFE to 110 (+10 => 2R).
    assert history[-1]["mae_r"] == 0.8
    assert history[-1]["mfe_r"] == 2.0


# ---------------------------------------------------------------------------
# Journal schema + log_exit population
# ---------------------------------------------------------------------------


def test_journal_schema_has_excursion_columns() -> None:
    for col in ("mae_pct", "mfe_pct", "mae_r", "mfe_r"):
        assert col in SCHEMA_COLUMNS


def test_log_exit_writes_excursion_columns(tmp_data_dir: Path) -> None:
    logger = TradeLogger(str(tmp_data_dir))
    logger.log_entry(_order(entry=100.0, stop=95.0), fill_price=100.0)

    event = ExitEvent(
        symbol="AAPL",
        exit_price=112.0,
        exit_reason=ExitReason.TARGET_HIT,
        exit_date=datetime.now(),
        pnl_gross=120.0,
        mae_pct=0.04,
        mfe_pct=0.12,
        mae_r=0.8,
        mfe_r=2.4,
    )
    logger.log_exit("AAPL", event)

    df = pd.read_csv(logger.csv_path, dtype=str)
    row = df[df["symbol"] == "AAPL"].iloc[-1]
    assert float(row["mae_r"]) == 0.8
    assert float(row["mfe_r"]) == 2.4
    assert float(row["mfe_pct"]) == 0.12


def test_log_exit_blank_excursion_when_absent(tmp_data_dir: Path) -> None:
    logger = TradeLogger(str(tmp_data_dir))
    logger.log_entry(_order(), fill_price=100.0)
    event = ExitEvent(
        symbol="AAPL", exit_price=101.0, exit_reason=ExitReason.MANUAL,
        exit_date=datetime.now(), pnl_gross=10.0,
    )
    logger.log_exit("AAPL", event)
    df = pd.read_csv(logger.csv_path, dtype=str)
    row = df[df["symbol"] == "AAPL"].iloc[-1]
    # Missing excursion -> blank cell (NaN when read back), not a crash.
    assert pd.isna(row["mae_r"]) or row["mae_r"] == ""


# ---------------------------------------------------------------------------
# Autotune advisory integration
# ---------------------------------------------------------------------------


def test_autotune_surfaces_excursion_advisories() -> None:
    # 12 winners that each left ~2R on the table -> targets-too-conservative.
    rows = [
        {"pnl_net": "50", "r_multiple": "1.0", "mae_r": "0.3", "mfe_r": "3.0",
         "mfe_pct": "0.09", "mae_pct": "0.01",
         "exit_time": f"2026-06-{i % 28 + 1:02d}T15:00:00"}
        for i in range(12)
    ]
    df = pd.DataFrame(rows)
    result = tune(df, Settings(AUTOTUNE_ENABLED=True, AUTOTUNE_MIN_TRADES=10))
    ids = {a["id"] for a in result.advisories}
    assert "targets_too_conservative" in ids
    assert "advisories" in result.to_dict()


def test_autotune_advisories_empty_without_excursion_data() -> None:
    df = pd.DataFrame({
        "pnl_net": ["10", "-5", "10"],
        "exit_time": ["2026-06-01T15:00:00", "2026-06-02T15:00:00",
                      "2026-06-03T15:00:00"],
    })
    result = tune(df, Settings(AUTOTUNE_ENABLED=True))
    assert result.advisories == []
