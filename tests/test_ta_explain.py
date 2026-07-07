"""Tests for the deterministic TA explanation templates (dashboard/ta_explain)."""

from __future__ import annotations

import pytest

from dashboard.ta_explain import (
    ai_note_from_criteria,
    build_explanation,
    current_explanation,
    entry_explanation,
    setup_explanation,
    stop_explanation,
    target_explanation,
)

ALL_STRATEGIES = ["vcp_breakout", "momentum", "swing", "mean_reversion", "pead"]
PATTERN_WORDS = {
    "vcp_breakout": "volatility-contraction",
    "momentum": "trend-continuation",
    "swing": "pullback",
    "mean_reversion": "mean-reversion",
    "pead": "post-earnings",
}


# --------------------------------------------------------------------------- #
# setup — must render for all five strategies
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("strategy", ALL_STRATEGIES)
def test_setup_explanation_all_strategies(strategy: str) -> None:
    text = setup_explanation("A", strategy)
    assert text.startswith("Grade A ")
    assert PATTERN_WORDS[strategy] in text
    assert "full position size" in text


def test_setup_explanation_grade_b_meaning() -> None:
    assert "75% position size" in setup_explanation("B", "momentum")


def test_setup_explanation_unknown_strategy() -> None:
    text = setup_explanation("A", "weird")
    assert "weird setup" in text


# --------------------------------------------------------------------------- #
# stop
# --------------------------------------------------------------------------- #


def test_stop_explanation_atr_formula() -> None:
    # 351.28 - 1.5 * 9.18 = 337.51 — the spec's worked example.
    text = stop_explanation(351.28, 337.51, 9.18, 1.5)
    assert "ATR-based stop" in text
    assert "$351.28" in text
    assert "$9.18" in text
    assert "1.5" in text
    assert "$337.51" in text


def test_stop_explanation_custom_level() -> None:
    text = stop_explanation(100.0, 80.0, 2.0, 1.5)  # 100 - 3 != 80
    assert "custom level" in text
    assert "20.0% below" in text
    assert "$2.00" in text  # ATR still surfaced


def test_stop_explanation_missing_stop() -> None:
    assert "No stop price" in stop_explanation(100.0, 0.0, 2.0, 1.5)


# --------------------------------------------------------------------------- #
# target
# --------------------------------------------------------------------------- #


def test_target_explanation_rr_formula() -> None:
    # entry 100, stop 90, target 118 → 1.8R
    text = target_explanation(100.0, 90.0, 118.0, 1.8)
    assert "$118.00" in text
    assert "1.8:1" in text


def test_target_explanation_notes_resistance_before_target() -> None:
    resistance = [{"price": 110.0, "touches": 4}]
    text = target_explanation(100.0, 90.0, 118.0, 1.8, resistance)
    assert "resistance $110.00" in text
    assert "4 touches" in text


def test_target_explanation_ignores_resistance_beyond_target() -> None:
    resistance = [{"price": 130.0, "touches": 4}]
    text = target_explanation(100.0, 90.0, 118.0, 1.8, resistance)
    assert "resistance" not in text


# --------------------------------------------------------------------------- #
# entry
# --------------------------------------------------------------------------- #


def test_entry_explanation_from_state() -> None:
    trade = {"signal_strength": 0.81, "grade": "A"}
    state = {
        "rsi_value": 61.0, "rsi_momentum_zone": True,
        "macd_confirmed_bullish": True,
        "volume_ratio": 2.1, "volume_surge": True,
        "bullish_stack": True, "obv_confirming": True,
    }
    text = entry_explanation(trade, state)
    assert "combined score 0.81 (grade A)" in text
    assert "RSI 61 in the momentum zone" in text
    assert "MACD bullish crossover confirmed above zero" in text
    assert "volume 2.1×" in text
    assert "all four EMAs" in text
    assert "OBV confirming" in text


def test_entry_explanation_falls_back_to_trade_scalars() -> None:
    trade = {
        "overall_score": 7.5, "grade": "B",
        "rsi_value": 55.0, "volume_ratio": 1.3, "macd_histogram": 0.2,
    }
    text = entry_explanation(trade, {})
    assert "0.75 (grade B)" in text
    assert "RSI 55" in text
    assert "volume 1.3×" in text


# --------------------------------------------------------------------------- #
# current readings + AI note
# --------------------------------------------------------------------------- #


def test_current_explanation_open_position() -> None:
    live = {
        "price": 151.20, "change_pct": 1.7, "rsi": 58.0,
        "macd_hist": 0.12, "pct_above_ema20": 2.1, "r_progress": 0.62,
        "distance_to_stop_pct": 5.9, "distance_to_target_pct": 5.8,
    }
    text = current_explanation(live)
    assert text.startswith("Now: ")
    assert "$151.20" in text
    assert "RSI 58" in text
    assert "MACD histogram positive" in text
    assert "2.1% above the 20 EMA" in text
    assert "+0.62R" in text
    assert "5.9% from stop, 5.8% from target" in text


def test_current_explanation_empty() -> None:
    assert current_explanation({}) == ""


def test_ai_note_from_criteria() -> None:
    criteria = [
        {"key": "setup_grade", "explanation": "x"},
        {"key": "ai_veto", "explanation": "APPROVE — no negative news"},
    ]
    assert ai_note_from_criteria(criteria) == "AI veto: APPROVE — no negative news"
    assert ai_note_from_criteria([]) is None
    assert ai_note_from_criteria(None) is None


# --------------------------------------------------------------------------- #
# full block
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("strategy", ALL_STRATEGIES)
def test_build_explanation_renders_all_sections(strategy: str) -> None:
    trade = {
        "entry_price": 148.60, "stop_price": 142.30, "target_price": 159.94,
        "grade": "A", "strategy": strategy, "signal_strength": 0.81,
    }
    block = build_explanation(
        trade,
        {"rsi_value": 61.0},
        4.2,
        {"support": [], "resistance": []},
        atr_multiplier=1.5,
        rr_min=1.8,
    )
    assert set(block.keys()) == {
        "entry", "stop", "target", "setup", "current", "ai_note"
    }
    assert block["entry"]
    assert "$142.30" in block["stop"]
    assert "$159.94" in block["target"]
    assert PATTERN_WORDS[strategy] in block["setup"]
    assert block["current"] is None  # no live block for closed trades
