"""Tests for trade rationale capture (monitoring F9)."""

from __future__ import annotations

from pathlib import Path

from journal.rationale import (
    RationaleStore,
    build_trade_rationale,
    find_rationale,
    read_rationales,
)


class _FakeAI:
    decision = "APPROVE"
    reasoning = "No adverse news in the lookback window."
    tier = "tier2"


class _FakeRegime:
    regime = "bull"


def test_build_criteria_shape(sample_signal):
    criteria = build_trade_rationale(
        sample_signal,
        ai_decision=_FakeAI(),
        regime=_FakeRegime(),
        regime_multiplier=1.1,
        risk_reward_min=1.8,
    )
    # Spec: 5–10 named criteria, scores 0–10, non-empty explanations.
    assert 5 <= len(criteria) <= 10
    for c in criteria:
        assert 0.0 <= c["score"] <= 10.0, c
        assert c["name"]
        assert c["explanation"]
    keys = {c["key"] for c in criteria}
    assert {"setup_grade", "breakout_pattern", "volume_confirmation",
            "risk_reward", "ai_veto", "regime_fit"} <= keys
    # The AI decision text actually returned is preserved.
    ai = next(c for c in criteria if c["key"] == "ai_veto")
    assert "No adverse news" in ai["explanation"]


def test_build_without_optional_context(sample_signal):
    criteria = build_trade_rationale(sample_signal)
    keys = {c["key"] for c in criteria}
    assert "ai_veto" not in keys and "regime_fit" not in keys
    assert len(criteria) >= 5


def test_store_roundtrip_and_find(tmp_data_dir: Path, sample_signal):
    store = RationaleStore(tmp_data_dir)
    criteria = build_trade_rationale(sample_signal)
    store.record(sample_signal, criteria, quantity=15, entry_price=195.7,
                 entry_time="2026-07-07T10:30:00",
                 bars=[{"t": "2026-07-04", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}])
    store.record(sample_signal, criteria, quantity=5, entry_price=200.0,
                 entry_time="2026-07-01T10:30:00", bars=[])

    records = read_rationales(tmp_data_dir)
    assert len(records) == 2
    assert records[0]["entry_price"] == 200.0  # newest write first

    # Nearest entry_time wins.
    rec = find_rationale(tmp_data_dir, "AAPL", "2026-07-07T10:35:00")
    assert rec["entry_price"] == 195.7
    assert rec["bars"] and rec["bars"][0]["c"] == 1.5
    # Levels are the entry-time levels.
    assert rec["stop_price"] == sample_signal.stop_price
    assert rec["target_price"] == sample_signal.target_price

    # No entry_time → newest for the symbol.
    assert find_rationale(tmp_data_dir, "aapl") is not None
    # Unknown symbol → None (renders "no rationale captured", never errors).
    assert find_rationale(tmp_data_dir, "ZZZZ") is None


def test_write_failure_never_raises(tmp_path: Path, sample_signal):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    store = RationaleStore(blocker / "sub")
    store.record(sample_signal, [], quantity=1, entry_price=1.0)  # must not raise


def test_short_direction_recorded(tmp_data_dir: Path, sample_signal):
    sample_signal.direction = "short"
    store = RationaleStore(tmp_data_dir)
    store.record(sample_signal, [], quantity=1, entry_price=100.0)
    rec = find_rationale(tmp_data_dir, sample_signal.symbol)
    assert rec["direction"] == "short"


def test_corrupt_lines_skipped(tmp_data_dir: Path, sample_signal):
    store = RationaleStore(tmp_data_dir)
    store.record(sample_signal, [], quantity=1, entry_price=1.0)
    with open(store.path, "a", encoding="utf-8") as f:
        f.write("not-json\n")
    assert len(read_rationales(tmp_data_dir)) == 1
