"""Tests for MAE/MFE excursion analytics (capture + distribution + advisories)."""

from __future__ import annotations

import pandas as pd
import pytest

from analytics.excursion import (
    analyze_stop_target_efficiency,
    compute_excursion_stats,
    excursion_metrics,
    excursion_report,
    histogram,
    initial_risk_per_share,
    percentile_summary,
    position_excursion,
    records_from_dataframe,
    update_excursion,
)


# ---------------------------------------------------------------------------
# initial_risk_per_share
# ---------------------------------------------------------------------------


def test_initial_risk_long_and_short():
    assert initial_risk_per_share(100.0, 95.0, "long") == pytest.approx(5.0)
    assert initial_risk_per_share(100.0, 108.0, "short") == pytest.approx(8.0)


def test_initial_risk_undefined_when_stop_wrong_side():
    # A long stop above entry, or zero distance, has no positive risk.
    assert initial_risk_per_share(100.0, 105.0, "long") is None
    assert initial_risk_per_share(100.0, 100.0, "long") is None
    assert initial_risk_per_share("junk", 90.0, "long") is None


# ---------------------------------------------------------------------------
# update_excursion — ratcheting
# ---------------------------------------------------------------------------


def test_update_excursion_long_ratchets_extremes():
    pos = {"entry_price": 100.0, "direction": "long"}
    # First bar: dips to 97, peaks at 102.
    update_excursion(pos, high=102.0, low=97.0)
    assert pos["mfe_price"] == 102.0
    assert pos["mae_price"] == 97.0
    # A milder bar must NOT shrink the recorded extremes.
    update_excursion(pos, high=101.0, low=99.0)
    assert pos["mfe_price"] == 102.0
    assert pos["mae_price"] == 97.0
    # A wider bar widens both.
    update_excursion(pos, high=105.0, low=94.0)
    assert pos["mfe_price"] == 105.0
    assert pos["mae_price"] == 94.0


def test_update_excursion_short_inverts_direction():
    pos = {"entry_price": 100.0, "direction": "short"}
    # For a short, favourable = price falling (low), adverse = price rising (high).
    update_excursion(pos, high=103.0, low=96.0)
    assert pos["mfe_price"] == 96.0  # best = lowest low
    assert pos["mae_price"] == 103.0  # worst = highest high


def test_update_excursion_single_side_and_noop():
    pos = {"entry_price": 50.0, "direction": "long"}
    # Only a last price known -> treated as a single point.
    update_excursion(pos, high=None, low=48.0)
    assert pos["mae_price"] == 48.0
    assert pos["mfe_price"] == 50.0  # seeded from entry, never rose
    # No price at all -> untouched.
    snapshot = dict(pos)
    update_excursion(pos, high=None, low=None)
    assert pos == snapshot
    # No entry price -> no-op.
    empty: dict = {}
    update_excursion(empty, high=1.0, low=1.0)
    assert "mae_price" not in empty


# ---------------------------------------------------------------------------
# excursion_metrics / position_excursion
# ---------------------------------------------------------------------------


def test_excursion_metrics_long_pct_and_r():
    # entry 100, stop 95 -> risk 5/share. MAE to 96 (-4), MFE to 110 (+10).
    m = excursion_metrics(100.0, 95.0, "long", mae_price=96.0, mfe_price=110.0)
    assert m["mae_pct"] == pytest.approx(0.04)
    assert m["mfe_pct"] == pytest.approx(0.10)
    assert m["mae_r"] == pytest.approx(0.8)   # 4 / 5
    assert m["mfe_r"] == pytest.approx(2.0)   # 10 / 5


def test_excursion_metrics_short():
    # short entry 100, stop 108 -> risk 8. Adverse rises to 104 (+4), fav to 90 (10).
    m = excursion_metrics(100.0, 108.0, "short", mae_price=104.0, mfe_price=90.0)
    assert m["mae_pct"] == pytest.approx(0.04)
    assert m["mfe_pct"] == pytest.approx(0.10)
    assert m["mae_r"] == pytest.approx(0.5)   # 4 / 8
    assert m["mfe_r"] == pytest.approx(1.25)  # 10 / 8


def test_excursion_metrics_r_none_when_risk_undefined():
    m = excursion_metrics(100.0, 100.0, "long", mae_price=98.0, mfe_price=105.0)
    assert m["mae_r"] is None and m["mfe_r"] is None
    # pct still computed off entry.
    assert m["mae_pct"] == pytest.approx(0.02)


def test_position_excursion_prefers_original_stop():
    # A ratcheted live stop (98) must not shrink the risk denominator; the
    # entry-time stop (90) is used instead.
    pos = {
        "entry_price": 100.0, "direction": "long",
        "stop_price": 98.0, "original_stop_loss": 90.0,
        "mae_price": 95.0, "mfe_price": 120.0,
    }
    m = position_excursion(pos)
    assert m["mae_r"] == pytest.approx(0.5)   # 5 / 10, not 5 / 2
    assert m["mfe_r"] == pytest.approx(2.0)   # 20 / 10


# ---------------------------------------------------------------------------
# percentile_summary / histogram
# ---------------------------------------------------------------------------


def test_percentile_summary_empty_and_values():
    empty = percentile_summary([])
    assert empty["count"] == 0 and empty["p50"] == 0.0
    s = percentile_summary([0.0, 1.0, 2.0, 3.0, 4.0])
    assert s["count"] == 5
    assert s["p50"] == pytest.approx(2.0)
    assert s["max"] == pytest.approx(4.0)
    # NaN / inf are dropped.
    s2 = percentile_summary([1.0, float("nan"), float("inf"), 3.0])
    assert s2["count"] == 2


def test_histogram_bins_counts_and_tail_clamp():
    h = histogram([0.1, 0.15, 0.4, 0.42, 0.9], 0.25)
    assert h["bins"][0] == 0.0
    assert sum(h["counts"]) == 5
    # Empty / non-positive width -> empty.
    assert histogram([], 0.25) == {"bins": [], "counts": []}
    assert histogram([0.1], 0.0) == {"bins": [], "counts": []}
    # Values at max_edge land in the final bin, not a new one.
    h2 = histogram([0.0, 1.0], 0.5, max_edge=1.0)
    assert sum(h2["counts"]) == 2


# ---------------------------------------------------------------------------
# compute_excursion_stats
# ---------------------------------------------------------------------------


def test_compute_excursion_stats_splits_winners_losers():
    records = [
        {"mae_pct": 0.02, "mfe_pct": 0.10, "mae_r": 0.4, "mfe_r": 2.0, "pnl_net": 50},
        {"mae_pct": 0.05, "mfe_pct": 0.01, "mae_r": 1.0, "mfe_r": 0.2, "pnl_net": -30},
        {"mae_pct": 0.03, "mfe_pct": 0.08, "mae_r": 0.6, "mfe_r": 1.5, "pnl_net": 20},
    ]
    stats = compute_excursion_stats(records)
    assert stats["trades"] == 3
    assert stats["winners"]["count"] == 2
    assert stats["losers"]["count"] == 1
    assert stats["mae_r"]["summary"]["count"] == 3
    assert stats["mfe_r"]["histogram"]["counts"]


# ---------------------------------------------------------------------------
# analyze_stop_target_efficiency
# ---------------------------------------------------------------------------


def test_stops_too_tight_flagged():
    # 10 winners, most of which dipped to within 0.85R of the stop first.
    winners = [
        {"pnl_net": 10, "mae_r": 0.95, "mfe_r": 2.0, "r_multiple": 2.0}
        for _ in range(8)
    ] + [
        {"pnl_net": 10, "mae_r": 0.2, "mfe_r": 2.0, "r_multiple": 2.0}
        for _ in range(2)
    ]
    result = analyze_stop_target_efficiency(winners)
    ids = {a["id"] for a in result["advisories"] if a["severity"] == "warn"}
    assert "stops_too_tight" in ids


def test_targets_too_conservative_flagged():
    # 10 winners that each realised ~1R but peaked at ~3R -> ~2R left on table.
    winners = [
        {"pnl_net": 10, "mae_r": 0.3, "mfe_r": 3.0, "r_multiple": 1.0}
        for _ in range(10)
    ]
    result = analyze_stop_target_efficiency(winners)
    ids = {a["id"] for a in result["advisories"] if a["severity"] == "warn"}
    assert "targets_too_conservative" in ids


def test_stops_giveback_flagged():
    # Stopped-out losers that had run up past 1R before reversing.
    losers = [
        {"pnl_net": -10, "mae_r": 1.0, "mfe_r": 1.5, "exit_reason": "STOP_LOSS"}
        for _ in range(10)
    ]
    result = analyze_stop_target_efficiency(losers)
    ids = {a["id"] for a in result["advisories"] if a["severity"] == "warn"}
    assert "stops_giveback" in ids


def test_insufficient_sample_is_info_not_warn():
    result = analyze_stop_target_efficiency(
        [{"pnl_net": 10, "mae_r": 0.9, "mfe_r": 2.0, "r_multiple": 2.0}]
    )
    warns = [a for a in result["advisories"] if a["severity"] == "warn"]
    infos = [a for a in result["advisories"] if a["severity"] == "info"]
    assert not warns and infos


def test_well_calibrated_produces_no_warnings():
    # Winners that barely dipped and captured most of their move.
    winners = [
        {"pnl_net": 10, "mae_r": 0.2, "mfe_r": 1.1, "r_multiple": 1.0}
        for _ in range(12)
    ]
    result = analyze_stop_target_efficiency(winners)
    assert not [a for a in result["advisories"] if a["severity"] == "warn"]


# ---------------------------------------------------------------------------
# records_from_dataframe / excursion_report
# ---------------------------------------------------------------------------


def test_records_from_dataframe_drops_pre_excursion_rows():
    df = pd.DataFrame({
        "mae_pct": ["0.02", ""],
        "mfe_pct": ["0.10", ""],
        "mae_r": ["0.4", ""],
        "mfe_r": ["2.0", ""],
        "pnl_net": ["50", "-10"],
    })
    records = records_from_dataframe(df)
    assert len(records) == 1  # the blank-excursion row is dropped


def test_records_from_dataframe_empty():
    assert records_from_dataframe(pd.DataFrame()) == []
    assert records_from_dataframe(None) == []


def test_excursion_report_shape():
    records = [
        {"mae_pct": 0.02, "mfe_pct": 0.10, "mae_r": 0.4, "mfe_r": 2.0, "pnl_net": 50},
    ]
    report = excursion_report(records)
    assert report["trades"] == 1
    assert "distributions" in report and "efficiency" in report
    assert "mae_r" in report["distributions"]
