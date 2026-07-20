"""Tests for ETF leverage / inverse sub-classification (Gap 3)."""

from __future__ import annotations

import pytest

from config.etf_classification import (
    INVERSE,
    LEVERAGED_2X,
    LEVERAGED_3X,
    LEVERAGED_INVERSE,
    REGULAR,
    classify_leverage,
    default_risk_params,
    is_geared,
    leverage_label,
)


# ── static known map ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("TQQQ", LEVERAGED_3X),
        ("UPRO", LEVERAGED_3X),
        ("SPXL", LEVERAGED_3X),
        ("SOXL", LEVERAGED_3X),
        ("SQQQ", LEVERAGED_INVERSE),   # 3x inverse
        ("SPXS", LEVERAGED_INVERSE),   # 3x inverse
        ("SPXU", LEVERAGED_INVERSE),
        ("SDS", LEVERAGED_INVERSE),    # 2x inverse
        ("QLD", LEVERAGED_2X),
        ("SSO", LEVERAGED_2X),
        ("UVXY", LEVERAGED_2X),        # leveraged long vol
        ("SH", INVERSE),               # 1x inverse
        ("PSQ", INVERSE),
    ],
)
def test_known_leveraged_map(symbol, expected):
    assert classify_leverage(symbol) == expected


def test_case_insensitive():
    assert classify_leverage("tqqq") == LEVERAGED_3X
    assert classify_leverage(" sqqq ") == LEVERAGED_INVERSE


def test_plain_etf_is_regular():
    assert classify_leverage("SPY") == REGULAR
    assert classify_leverage("VTI") == REGULAR
    assert classify_leverage("") == REGULAR


# ── name-based heuristics (from a yfinance info dict) ────────────────────────


@pytest.mark.parametrize(
    "name,expected",
    [
        ("ProShares UltraPro QQQ", LEVERAGED_3X),
        ("Direxion Daily S&P 500 Bull 3X Shares", LEVERAGED_3X),
        ("ProShares Ultra S&P500", LEVERAGED_2X),
        ("Direxion Daily Financial Bear 3X", LEVERAGED_INVERSE),
        ("ProShares UltraShort S&P500", LEVERAGED_INVERSE),  # 2x inverse
        ("ProShares Short S&P500", INVERSE),                  # 1x inverse
        ("ProShares UltraPro Short QQQ", LEVERAGED_INVERSE),  # 3x inverse
    ],
)
def test_name_heuristics(name, expected):
    # An unknown ticker forces the name path.
    assert classify_leverage("ZZZZ", info={"longName": name}) == expected


def test_short_term_futures_not_flagged_inverse():
    # "Short-Term" is a futures maturity, not a direction — must not read as inverse.
    cat = classify_leverage("ZZVL", info={"longName": "Fund Ultra VIX Short-Term Futures"})
    # Ultra → 2x long, and it should NOT be classified inverse.
    assert cat == LEVERAGED_2X


def test_plain_name_stays_regular():
    assert classify_leverage("ZZZZ", info={"longName": "Vanguard Total Stock Market"}) == REGULAR


def test_explicit_leverage_factor():
    assert classify_leverage("AAAA", info={"leverageFactor": 3}) == LEVERAGED_3X
    assert classify_leverage("BBBB", info={"leverageFactor": -3}) == LEVERAGED_INVERSE
    assert classify_leverage("CCCC", info={"leverageFactor": 2}) == LEVERAGED_2X
    assert classify_leverage("DDDD", info={"leverageFactor": -1}) == INVERSE
    assert classify_leverage("EEEE", info={"leverageFactor": 1}) == REGULAR


def test_static_map_wins_over_info():
    # A curated ticker keeps its classification even if info says otherwise.
    assert classify_leverage("TQQQ", info={"longName": "Totally Plain Fund"}) == LEVERAGED_3X


# ── helpers ──────────────────────────────────────────────────────────────────


def test_is_geared():
    assert is_geared("TQQQ")
    assert is_geared("SQQQ")
    assert not is_geared("SPY")
    assert not is_geared("VOO")


def test_leverage_label():
    assert leverage_label(LEVERAGED_3X) == "3x Leveraged"
    assert leverage_label(LEVERAGED_INVERSE) == "Leveraged Inverse"
    assert leverage_label(REGULAR) == ""


def test_default_risk_params():
    assert default_risk_params(LEVERAGED_2X) == (0.5, 0.05)
    assert default_risk_params(LEVERAGED_3X) == (0.33, 0.03)
    assert default_risk_params(INVERSE) == (0.7, 0.05)
    assert default_risk_params(LEVERAGED_INVERSE) == (0.25, 0.02)
    assert default_risk_params(REGULAR) is None
