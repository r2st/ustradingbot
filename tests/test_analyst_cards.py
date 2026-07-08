"""Tests for the Analyst card builder (UX spec: self-explanatory cards)."""

from __future__ import annotations

import pytest

from config.settings import Settings
from dashboard.analyst_cards import (
    PLAIN_LANGUAGE,
    build_conditions,
    build_position_card,
    build_stress_test,
    build_watchlist_card,
    explain_rejection,
    hypothetical_shares,
)


def _bullish_indicators() -> dict:
    return {
        "price": 314.40, "rsi": 63.0, "rsi_rising": True,
        "rsi_overbought": False, "rsi_momentum_zone": True,
        "macd_hist": 0.4, "macd_hist_expanding": True, "macd_bullish": True,
        "ema9": 310.0, "ema20": 299.13, "ema50": 290.0, "ema200": 260.0,
        "above_ema20": True, "above_ema50": True, "above_ema200": True,
        "bullish_stack": True, "volume_ratio": 0.3, "obv_confirming": False,
        "above_vwap": True, "fast_cloud_bullish": True,
        "slow_cloud_bullish": True, "squeeze_hint": False,
        "atr14": 5.2, "atr_pct": 1.7,
    }


def _levels() -> dict:
    return {
        "support": [{"price": 263.22, "touches": 5}],
        "resistance": [{"price": 317.17, "touches": 2}],
    }


def _position_row(**overrides) -> dict:
    row = {
        "symbol": "AAPL", "side": "long", "strategy": "momentum",
        "grade": "B", "quantity": 100, "entry_price": 299.13,
        "stop_price": 300.80, "target_price": 337.87,
        "current_price": 314.40, "unrealized_pnl": 1527.0,
        "unrealized_pct": 5.1, "distance_to_stop_pct": 4.3,
        "distance_to_target_pct": 7.5, "r_progress": 0.9,
        "entry_time": "2026-07-07 16:03", "indicators": _bullish_indicators(),
        "key_levels": _levels(), "source": "template",
    }
    row.update(overrides)
    return row


# ------------------------------------------------------------- structure


def test_position_card_has_all_sections():
    card = build_position_card(_position_row())
    assert card["kind"] == "position"
    assert card["identity"]["symbol"] == "AAPL"
    assert card["identity"]["tag"] == "Momentum position"
    assert card["summary"]  # one plain sentence
    assert card["conditions"]["total"] == 5
    assert len(card["reasoning"]) == 4
    assert [r["category"] for r in card["reasoning"]] == [
        "trend", "momentum", "volume", "structure"
    ]
    assert card["invalidation"]
    assert card["stress_test"] is not None
    assert card["detail"]["stop_price"] == 300.80
    assert card["data_state"] == "live"


def test_no_verdict_words_or_bare_scores():
    """Spec section 7: no BUY/SELL/HOLD headlines, no bare confidence %."""
    import json

    card = build_position_card(_position_row())
    text = json.dumps(card).upper()
    for verdict in ("STRONG BUY", '"BUY"', '"SELL"', '"HOLD"'):
        assert verdict not in text
    assert "CONFIDENCE" not in text
    assert "READINESS" not in text


def test_reasoning_plain_language_leads():
    card = build_position_card(_position_row())
    trend = card["reasoning"][0]
    assert trend["plain_statement"].startswith("Trend:")
    assert "20-day average" in trend["supporting_stat"]
    momentum = card["reasoning"][1]
    # The stat explains RSI inline (no unexplained acronym).
    assert "RSI at 63" in momentum["supporting_stat"]
    assert "stretched" in momentum["supporting_stat"]


def test_invalidation_cites_support_level():
    card = build_position_card(_position_row())
    joined = " ".join(card["invalidation"])
    assert "263.22" in joined
    assert "held 5 time" in joined


def test_conditions_counts_match_items():
    conds = build_conditions(_bullish_indicators(), is_short=False)
    assert conds["total"] == len(conds["items"]) == 5
    assert conds["met"] == sum(1 for i in conds["items"] if i["met"])
    # volume_ratio 0.3 and no OBV confirmation -> volume check not met.
    vol = next(i for i in conds["items"] if i["key"] == "volume")
    assert vol["met"] is False


def test_data_state_unavailable_without_indicators():
    card = build_position_card(_position_row(indicators=None))
    assert card["data_state"] == "unavailable"
    assert "price history" in card["data_state_message"]
    assert card["reasoning"] == []


def test_data_state_degraded_marks_card():
    card = build_position_card(_position_row(), ai_degraded=True)
    assert card["data_state"] == "degraded"
    msg = card["data_state_message"]
    assert "rule-based" in msg
    # Plain language, no raw provider talk.
    assert "401" not in msg and "OpenRouter" not in msg


# ------------------------------------------------------------ stress test


def test_stress_test_math_long():
    st = build_stress_test(
        entry=100.0, shares=10, stop=95.0, target=110.0, current=102.0,
        levels={"support": [{"price": 97.0, "touches": 3}],
                "resistance": [{"price": 105.0, "touches": 2}]},
        is_short=False, is_hypothetical=False,
    )
    assert st is not None
    by_label = {s["label"]: s for s in st["scenarios"]}
    assert by_label["Hits stop-loss"]["pnl_dollars"] == pytest.approx(-50.0)
    assert by_label["Hits stop-loss"]["r_multiple"] == pytest.approx(-1.0)
    assert by_label["Hits target"]["pnl_dollars"] == pytest.approx(100.0)
    assert by_label["Hits target"]["r_multiple"] == pytest.approx(2.0)
    assert by_label["Drops to support"]["pnl_dollars"] == pytest.approx(-30.0)
    assert by_label["No change"]["pnl_dollars"] == pytest.approx(20.0)
    # Ordered along the price line for the UI.
    prices = [s["trigger_price"] for s in st["scenarios"]]
    assert prices == sorted(prices)
    # Every scenario shows its working, traceable by hand.
    working = "\n".join(by_label["Drops to support"]["working"])
    assert "$97.00 - $100.00" in working
    assert "x 10" in working
    assert "R-multiple" in working


def test_stress_test_math_short_flips_direction():
    st = build_stress_test(
        entry=100.0, shares=10, stop=105.0, target=90.0, current=100.0,
        levels={"support": [], "resistance": []},
        is_short=True, is_hypothetical=False,
    )
    by_label = {s["label"]: s for s in st["scenarios"]}
    # A rise to the (buy-)stop is the loss; a drop to target is the gain.
    assert by_label["Hits stop-loss"]["pnl_dollars"] == pytest.approx(-50.0)
    assert by_label["Hits target"]["pnl_dollars"] == pytest.approx(100.0)
    # The panel states the direction flip plainly (spec 5.1).
    assert any("short" in n.lower() and "fall" in n.lower()
               for n in st["notes"])


def test_stress_test_never_implies_probability():
    st = build_stress_test(
        entry=100.0, shares=10, stop=95.0, target=110.0, current=100.0,
        levels={}, is_short=False, is_hypothetical=False,
    )
    assert any("not a prediction" in n for n in st["notes"])


def test_stress_test_requires_entry_and_stop():
    assert build_stress_test(entry=None, shares=10, stop=95.0, target=None,
                             current=100.0, levels={}, is_short=False,
                             is_hypothetical=False) is None
    assert build_stress_test(entry=100.0, shares=10, stop=None, target=None,
                             current=100.0, levels={}, is_short=False,
                             is_hypothetical=False) is None


def test_stress_test_zero_shares_shows_per_share_math():
    st = build_stress_test(
        entry=1222.0, shares=0, stop=1180.0, target=1300.0, current=1222.0,
        levels={}, is_short=False, is_hypothetical=True,
    )
    assert st["shares"] == 1
    assert st["shares_are_per_share_illustration"] is True
    assert any("zero shares" in n for n in st["notes"])


# ------------------------------------------------------------- watchlist


def _watchlist_row(**overrides) -> dict:
    row = {
        "symbol": "NVDA", "status": "signal", "price": 100.0,
        "change_pct": 1.2,
        "signal": {"strategy": "momentum", "grade": "A", "strength": 0.8,
                   "entry": 100.0, "stop": 95.0, "target": 115.0},
        "last_rejection": None, "indicators": _bullish_indicators(),
        "key_levels": _levels(), "source": "template",
    }
    row.update(overrides)
    return row


def test_watchlist_card_hypothetical_stress_test(settings: Settings):
    card = build_watchlist_card(_watchlist_row(), settings)
    st = card["stress_test"]
    assert st is not None
    assert st["is_hypothetical"] is True
    # Sized by the strategy's own rule, not an arbitrary round number.
    expected = hypothetical_shares(100.0, 95.0, "A", False, settings)
    assert st["shares"] == max(expected, 1)
    assert card["detail"]["order_status"] == "No position open — analysis only"


def test_watchlist_card_without_signal_explains_missing_stress_test(
        settings: Settings):
    card = build_watchlist_card(_watchlist_row(signal=None, status="idle"),
                                settings)
    assert card["stress_test"] is None
    assert "No suggested trade yet" in card["stress_test_unavailable_reason"]


def test_watchlist_rejection_in_plain_language(settings: Settings):
    card = build_watchlist_card(
        _watchlist_row(
            status="rejected",
            signal=None,
            last_rejection={"gate": "order_build",
                            "detail": "position size rounded to zero shares",
                            "ts": "2026-07-08T10:00:00"},
        ),
        settings,
    )
    plain = card["status_plain"]
    assert plain is not None
    assert "zero shares" in plain
    assert "too small" in plain
    # Not the raw system message alone.
    assert plain != "position size rounded to zero shares"


def test_explain_rejection_covers_unknown_gate():
    out = explain_rejection({"gate": "mystery", "detail": "odd thing"})
    assert "odd thing" in out


# ------------------------------------------------------------- sizing


def test_hypothetical_shares_mirrors_sizing_rules(settings: Settings):
    # capital * risk% / risk-per-share, capped at 10% notional.
    shares_a = hypothetical_shares(100.0, 95.0, "A", False, settings)
    shares_b = hypothetical_shares(100.0, 95.0, "B", False, settings)
    assert shares_a > 0
    assert shares_b == int(shares_a * 0.75) or shares_b <= shares_a
    # Degenerate stop -> zero shares.
    assert hypothetical_shares(100.0, 100.0, "A", False, settings) == 0
    assert hypothetical_shares(100.0, 105.0, "A", False, settings) == 0
    # Shorts: stop above entry is valid.
    assert hypothetical_shares(100.0, 105.0, "A", True, settings) > 0


# ------------------------------------------------------- shared content


def test_shared_content_source_has_definitions():
    for key in ("rsi", "support", "resistance", "r_multiple", "stop",
                "target", "hypothetical"):
        assert PLAIN_LANGUAGE[key]["define"], key
