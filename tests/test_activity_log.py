"""Tests for the engine activity event stream (monitoring F4 + F8)."""

from __future__ import annotations

import json
from pathlib import Path

from journal.activity_log import (
    ACTIVITY_FILE,
    ActivityLogger,
    cycles_summary,
    read_activity,
    read_last_scan,
    write_last_scan,
)
from signals.signal_types import Grade, Signal


def _mk_signal(symbol: str = "NVDA") -> Signal:
    return Signal(
        symbol=symbol, strategy="momentum", entry_price=100.0,
        stop_price=95.0, target_price=110.0, signal_strength=0.8,
        grade=Grade.A,
    )


def test_log_and_read_roundtrip(tmp_data_dir: Path):
    logger = ActivityLogger(tmp_data_dir)
    logger.log("cycle_start", cycle_id="c1")
    logger.log("signal_rejected", cycle_id="c1", symbol="NVDA", gate="ai_veto",
               reason="bad news")
    logger.log("trade_placed", cycle_id="c1", symbol="AAPL", quantity=10)

    events = read_activity(tmp_data_dir)
    assert len(events) == 3
    # Newest first.
    assert events[0]["event"] == "trade_placed"
    assert events[-1]["event"] == "cycle_start"
    assert all("ts" in e for e in events)


def test_read_filters(tmp_data_dir: Path):
    logger = ActivityLogger(tmp_data_dir)
    logger.log("signal_rejected", cycle_id="c1", symbol="NVDA", gate="cash_check")
    logger.log("signal_rejected", cycle_id="c2", symbol="AAPL", gate="ai_veto")
    logger.log("trade_placed", cycle_id="c2", symbol="AAPL")

    assert len(read_activity(tmp_data_dir, event="signal_rejected")) == 2
    assert len(read_activity(tmp_data_dir, symbol="aapl")) == 2
    assert len(read_activity(tmp_data_dir, cycle_id="c2")) == 2
    assert len(read_activity(tmp_data_dir, event="trade_placed", symbol="NVDA")) == 0


def test_since_ts_incremental(tmp_data_dir: Path):
    logger = ActivityLogger(tmp_data_dir)
    logger.log("cycle_start", cycle_id="c1")
    first_ts = read_activity(tmp_data_dir)[0]["ts"]
    logger.log("cycle_complete", cycle_id="c1")
    newer = read_activity(tmp_data_dir, since_ts=first_ts)
    assert [e["event"] for e in newer] == ["cycle_complete"]


def test_malformed_lines_skipped(tmp_data_dir: Path):
    logger = ActivityLogger(tmp_data_dir)
    logger.log("cycle_start", cycle_id="c1")
    with open(logger.path, "a", encoding="utf-8") as f:
        f.write("{not json!!\n")
    logger.log("cycle_complete", cycle_id="c1")
    events = read_activity(tmp_data_dir)
    assert len(events) == 2


def test_writer_failure_is_swallowed(tmp_path: Path):
    # Point the logger at a path whose parent is a *file* — the mkdir/open
    # fails, but log() must not raise (telemetry never breaks trading).
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    logger = ActivityLogger(blocker / "sub")
    logger.log("cycle_start", cycle_id="c1")  # must not raise


def test_rotation(tmp_data_dir: Path, monkeypatch):
    import journal.activity_log as mod

    monkeypatch.setattr(mod, "MAX_ACTIVITY_BYTES", 200)
    logger = ActivityLogger(tmp_data_dir)
    for i in range(50):
        logger.log("cycle_start", cycle_id=f"c{i}", filler="x" * 40)
    main = Path(tmp_data_dir) / ACTIVITY_FILE
    assert main.stat().st_size < 2 * 200 + 400  # never ~2x the cap
    assert main.with_suffix(main.suffix + ".1").exists()
    # Backup contents still readable through read_activity.
    assert len(read_activity(tmp_data_dir, limit=1000)) > 2


def test_cycles_summary_rollup(tmp_data_dir: Path):
    logger = ActivityLogger(tmp_data_dir)
    logger.log("cycle_start", cycle_id="c1")
    logger.log("scan_complete", cycle_id="c1", symbols_scanned=40, signals_found=3)
    logger.log("signal_rejected", cycle_id="c1", symbol="A", gate="ai_veto")
    logger.log("signal_rejected", cycle_id="c1", symbol="B", gate="ai_veto")
    logger.log("signal_rejected", cycle_id="c1", symbol="C", gate="cash_check")
    logger.log("trade_placed", cycle_id="c1", symbol="D")
    logger.log("exit", cycle_id="c1", symbol="E")
    logger.log("cycle_complete", cycle_id="c1", signals_found=3, elapsed_seconds=12.5)
    logger.log("cycle_start", cycle_id="c2")

    cycles = cycles_summary(tmp_data_dir)
    assert [c["cycle_id"] for c in cycles] == ["c2", "c1"]
    c1 = cycles[1]
    assert c1["signals_found"] == 3
    assert c1["rejected_by_gate"] == {"ai_veto": 2, "cash_check": 1}
    assert c1["trades_placed"] == 1
    assert c1["exits"] == 1
    assert c1["elapsed_s"] == 12.5


def test_last_scan_roundtrip(tmp_data_dir: Path):
    write_last_scan(tmp_data_dir, "c9", [_mk_signal("NVDA"), _mk_signal("AAPL")])
    snap = read_last_scan(tmp_data_dir)
    assert snap["cycle_id"] == "c9"
    assert len(snap["signals"]) == 2
    row = snap["signals"][0]
    assert row["symbol"] == "NVDA"
    assert row["grade"] == "A"
    assert row["entry"] == 100.0


def test_last_scan_missing_and_corrupt(tmp_data_dir: Path):
    assert read_last_scan(tmp_data_dir) == {}
    (Path(tmp_data_dir) / "last_scan.json").write_text("{broken", encoding="utf-8")
    assert read_last_scan(tmp_data_dir) == {}
