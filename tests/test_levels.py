"""Tests for multi-level exit ladders (execution.levels)."""

from __future__ import annotations

import pytest

from execution.levels import (
    ExitLevel,
    LevelError,
    build_levels,
    nearest_price,
    normalize_side,
    parse_level_specs,
    reprice_stop_levels,
    split_shares,
)


# ------------------------------------------------------------------- side


@pytest.mark.parametrize("raw,expected", [
    ("buy", "long"), ("BUY", "long"), ("long", "long"),
    ("sell", "short"), ("Sell", "short"), ("short", "short"),
    (None, "long"), ("", "long"),
])
def test_normalize_side(raw, expected):
    assert normalize_side(raw) == expected


def test_normalize_side_rejects_junk():
    with pytest.raises(LevelError):
        normalize_side("hold")


# ------------------------------------------------------------------ parsing


def test_parse_prices_long_stops_sorted_nearest_first():
    specs = parse_level_specs(
        [{"price": 92}, {"price": 97}, {"price": 95}], "stop", "long", 100.0
    )
    assert [s["price"] for s in specs] == [97, 95, 92]


def test_parse_prices_long_targets_sorted_nearest_first():
    specs = parse_level_specs(
        [{"price": 115}, {"price": 105}], "target", "long", 100.0
    )
    assert [s["price"] for s in specs] == [105, 115]


def test_parse_percent_sign_is_normalised():
    # -3 and 3 both mean "3% on the losing side" for a stop.
    for pct in (-3, 3):
        specs = parse_level_specs([{"percent": pct}], "stop", "long", 100.0)
        assert specs[0]["price"] == 97.0
    specs = parse_level_specs([{"percent": 5}], "target", "long", 100.0)
    assert specs[0]["price"] == 105.0


def test_parse_percent_short_side():
    # Short: stops above entry, targets below.
    stops = parse_level_specs([{"percent": 3}], "stop", "short", 100.0)
    assert stops[0]["price"] == 103.0
    targets = parse_level_specs([{"percent": 5}], "target", "short", 100.0)
    assert targets[0]["price"] == 95.0


@pytest.mark.parametrize("kind,side,price", [
    ("stop", "long", 101),     # stop above entry on a long
    ("stop", "long", 100),     # stop at entry
    ("target", "long", 99),    # target below entry on a long
    ("stop", "short", 99),     # stop below entry on a short
    ("target", "short", 101),  # target above entry on a short
])
def test_parse_rejects_wrong_side_prices(kind, side, price):
    with pytest.raises(LevelError):
        parse_level_specs([{"price": price}], kind, side, 100.0)


def test_parse_rejects_duplicate_and_overweight():
    with pytest.raises(LevelError):
        parse_level_specs([{"price": 97}, {"price": 97}], "stop", "long", 100.0)
    with pytest.raises(LevelError):
        parse_level_specs(
            [{"price": 97, "pct": 60}, {"price": 95, "pct": 60}],
            "stop", "long", 100.0,
        )
    with pytest.raises(LevelError):
        parse_level_specs([{"price": 97, "pct": 0}], "stop", "long", 100.0)
    with pytest.raises(LevelError):
        parse_level_specs([{}], "stop", "long", 100.0)  # no price, no percent


# ------------------------------------------------------------- share split


def test_split_shares_explicit_with_remainder_to_last():
    assert split_shares(100, [33, 33, None]) == [33, 33, 34]
    assert split_shares(10, [33, 33, 34.0]) == [3, 3, 4]


def test_split_shares_even_when_unspecified():
    assert split_shares(90, [None, None, None]) == [30, 30, 30]


def test_split_shares_small_position_last_takes_all():
    # 1 share, 3 rungs: first two round to zero, the last takes the share.
    assert split_shares(1, [33, 33, None]) == [0, 0, 1]


def test_split_shares_total_always_covers_position():
    for qty in (1, 7, 10, 99, 1000):
        assert sum(split_shares(qty, [20, 30, None])) == qty


# ------------------------------------------------------------ build_levels


def test_build_levels_long_full_ladder():
    levels = build_levels(
        "buy", 100.0, 90,
        [{"percent": 3, "pct": 33}, {"percent": 5, "pct": 33}, {"percent": 8}],
        [{"percent": 5, "pct": 33}, {"percent": 10, "pct": 33}, {"percent": 15}],
    )
    stops = [l for l in levels if l.kind == "stop"]
    targets = [l for l in levels if l.kind == "target"]
    assert [s.price for s in stops] == [97.0, 95.0, 92.0]
    assert [t.price for t in targets] == [105.0, 110.0, 115.0]
    assert sum(s.quantity for s in stops) == 90
    assert sum(t.quantity for t in targets) == 90


def test_build_levels_requires_both_ladders():
    with pytest.raises(LevelError):
        build_levels("buy", 100.0, 10, [], [{"price": 105}])
    with pytest.raises(LevelError):
        build_levels("buy", 100.0, 10, [{"price": 97}], [])


def test_build_levels_serialisation_roundtrip():
    levels = build_levels("sell", 50.0, 10, [{"price": 52}], [{"price": 45}])
    restored = [ExitLevel.from_dict(l.to_dict()) for l in levels]
    assert restored == levels


# ---------------------------------------------------- extensibility hooks


def test_reprice_stop_levels_only_touches_untriggered_stops():
    levels = [
        ExitLevel("stop", 97.0, 3, triggered=True),
        ExitLevel("stop", 95.0, 3),
        ExitLevel("target", 105.0, 6),
    ]
    out = reprice_stop_levels(levels, {0: 96.0})
    assert out[0].price == 97.0      # triggered stop untouched
    assert out[1].price == 96.0      # first untriggered stop re-priced
    assert out[2].price == 105.0     # target untouched


def test_nearest_price_by_side():
    levels = [
        ExitLevel("stop", 97.0, 3), ExitLevel("stop", 95.0, 3),
        ExitLevel("target", 105.0, 3), ExitLevel("target", 110.0, 3),
    ]
    assert nearest_price(levels, "stop", "long") == 97.0
    assert nearest_price(levels, "target", "long") == 105.0
    # For a short the nearest stop is the lowest one above entry.
    assert nearest_price(levels, "stop", "short") == 95.0
    assert nearest_price(levels, "target", "short") == 110.0
