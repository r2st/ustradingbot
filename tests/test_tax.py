"""P1f — tax / realized-gains reporting (FIFO, ST/LT split, wash-sale)."""

from __future__ import annotations

import csv

import pandas as pd

from analytics import tax
from journal.trade_logger import SCHEMA_COLUMNS


def _row(**kw):
    r = {c: "" for c in SCHEMA_COLUMNS}
    r.update(kw)
    return r


def _write_journal(tmp_path, rows):
    data_dir = tmp_path
    path = data_dir / "trades.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SCHEMA_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    from analytics import performance
    performance.clear_trades_cache()
    return data_dir


def _trade(symbol, qty, entry_px, exit_px, entry_time, exit_time,
           direction="long", entry_comm=0.0, exit_comm=0.0):
    return _row(
        trade_id=f"{symbol}-{entry_time}", symbol=symbol, strategy="momentum",
        direction=direction, quantity=str(qty),
        entry_fill_price=str(entry_px), entry_time=entry_time,
        entry_commission=str(entry_comm), exit_price=str(exit_px),
        exit_time=exit_time, exit_commission=str(exit_comm),
        exit_reason="TARGET_HIT",
    )


# ---------------------------------------------------------------------------
# Per-lot maths + ST/LT boundary
# ---------------------------------------------------------------------------


def test_basic_gain_and_short_term(tmp_path):
    d = _write_journal(tmp_path, [
        _trade("AAPL", 10, 100.0, 115.0, "2026-01-05T10:00:00",
               "2026-03-05T10:00:00", entry_comm=1.0, exit_comm=1.0),
    ])
    rep = tax.build_tax_report(d).to_dict()
    lot = rep["lots"][0]
    assert lot["cost_basis"] == 1001.0        # 10*100 + 1
    assert lot["proceeds"] == 1149.0          # 10*115 - 1
    assert lot["gain"] == 148.0
    assert lot["term"] == "short"
    assert rep["summary"]["short_term"]["gain"] == 148.0
    assert rep["summary"]["long_term"]["count"] == 0
    assert rep["summary"]["total_gain"] == 148.0


def test_long_term_boundary_365_vs_366(tmp_path):
    # Exactly 365 days held → still short-term (needs MORE than one year).
    d = _write_journal(tmp_path, [
        _trade("AAA", 1, 100.0, 110.0, "2025-01-01T10:00:00",
               "2026-01-01T10:00:00"),  # 365 days
    ])
    assert tax.build_tax_report(d).to_dict()["lots"][0]["term"] == "short"

    d2 = _write_journal(tmp_path, [
        _trade("BBB", 1, 100.0, 110.0, "2025-01-01T10:00:00",
               "2026-01-02T10:00:00"),  # 366 days
    ])
    assert tax.build_tax_report(d2).to_dict()["lots"][0]["term"] == "long"


def test_short_position_always_short_term(tmp_path):
    d = _write_journal(tmp_path, [
        _trade("TSLA", 5, 300.0, 250.0, "2024-01-01T10:00:00",
               "2026-01-01T10:00:00", direction="short"),  # 2 years held
    ])
    lot = tax.build_tax_report(d).to_dict()["lots"][0]
    assert lot["term"] == "short"
    # Short: sold at 300, covered at 250 → gain 50*5 = 250
    assert lot["gain"] == 250.0


def test_losing_trade_negative_gain(tmp_path):
    d = _write_journal(tmp_path, [
        _trade("NVDA", 10, 200.0, 180.0, "2026-02-01T10:00:00",
               "2026-02-10T10:00:00"),
    ])
    lot = tax.build_tax_report(d).to_dict()["lots"][0]
    assert lot["gain"] == -200.0


# ---------------------------------------------------------------------------
# Wash sale
# ---------------------------------------------------------------------------


def test_wash_sale_flagged_when_repurchase_within_30d(tmp_path):
    d = _write_journal(tmp_path, [
        # Loss sale on 2026-02-10
        _trade("MSFT", 10, 300.0, 280.0, "2026-01-01T10:00:00",
               "2026-02-10T10:00:00"),
        # Repurchase 5 days later (entry within 30d of the loss sale)
        _trade("MSFT", 10, 285.0, 290.0, "2026-02-15T10:00:00",
               "2026-04-01T10:00:00"),
    ])
    rep = tax.build_tax_report(d).to_dict()
    assert rep["wash_sale_count"] == 1
    assert rep["total_disallowed_loss"] == 200.0
    ws = rep["wash_sales"][0]
    assert ws["symbol"] == "MSFT"
    assert ws["disallowed_loss"] == 200.0
    assert ws["replacement_date"].startswith("2026-02-15")


def test_wash_sale_not_flagged_when_repurchase_outside_30d(tmp_path):
    d = _write_journal(tmp_path, [
        _trade("MSFT", 10, 300.0, 280.0, "2026-01-01T10:00:00",
               "2026-02-10T10:00:00"),
        # Repurchase 40 days later
        _trade("MSFT", 10, 285.0, 290.0, "2026-03-25T10:00:00",
               "2026-05-01T10:00:00"),
    ])
    assert tax.build_tax_report(d).to_dict()["wash_sale_count"] == 0


def test_winning_trade_is_never_wash_sale(tmp_path):
    d = _write_journal(tmp_path, [
        _trade("AMD", 10, 100.0, 120.0, "2026-01-01T10:00:00",
               "2026-02-01T10:00:00"),
        _trade("AMD", 10, 118.0, 130.0, "2026-02-10T10:00:00",
               "2026-03-01T10:00:00"),
    ])
    assert tax.build_tax_report(d).to_dict()["wash_sale_count"] == 0


# ---------------------------------------------------------------------------
# Aggregation / filtering
# ---------------------------------------------------------------------------


def test_empty_journal_zeroed(tmp_path):
    d = _write_journal(tmp_path, [])
    rep = tax.build_tax_report(d).to_dict()
    assert rep["summary"]["total_gain"] == 0.0
    assert rep["lots"] == []
    assert rep["available_years"] == []


def test_per_year_filter(tmp_path):
    d = _write_journal(tmp_path, [
        _trade("AAA", 1, 100.0, 110.0, "2024-06-01T10:00:00",
               "2024-07-01T10:00:00"),
        _trade("BBB", 1, 100.0, 90.0, "2026-06-01T10:00:00",
               "2026-07-01T10:00:00"),
    ])
    full = tax.build_tax_report(d).to_dict()
    assert set(full["available_years"]) == {2024, 2026}
    assert len(full["lots"]) == 2

    only_2026 = tax.build_tax_report(d, year=2026).to_dict()
    assert only_2026["year"] == 2026
    assert len(only_2026["lots"]) == 1
    assert only_2026["summary"]["total_gain"] == -10.0


# ---------------------------------------------------------------------------
# FIFO matcher (multiple lots / partial sell)
# ---------------------------------------------------------------------------


def test_fifo_two_buys_one_partial_sell():
    # Directly exercise fifo_match with a hand-built event stream.
    def ev(side, day, qty, px):
        return tax._Event("AAA", side, pd.Timestamp(f"2026-01-{day:02d}").to_pydatetime(),
                          qty, px, 0.0, "long")

    events = [
        ev("buy", 1, 10, 100.0),   # lot 1: 10 @ 100
        ev("buy", 2, 10, 110.0),   # lot 2: 10 @ 110
        ev("sell", 3, 15, 130.0),  # sell 15 → 10 from lot1 + 5 from lot2
    ]
    lots = tax.fifo_match(events)
    assert len(lots) == 2
    # First realised lot: 10 @100 basis → proceeds 10*130
    assert lots[0].quantity == 10
    assert lots[0].cost_basis == 1000.0
    assert lots[0].proceeds == 1300.0
    assert lots[0].gain == 300.0
    # Second realised lot: 5 @110 basis → proceeds 5*130
    assert lots[1].quantity == 5
    assert lots[1].cost_basis == 550.0
    assert lots[1].proceeds == 650.0
    assert lots[1].gain == 100.0


def test_report_smoke_shape(tmp_path):
    d = _write_journal(tmp_path, [
        _trade("AAPL", 10, 100.0, 115.0, "2026-01-05T10:00:00",
               "2026-03-05T10:00:00"),
    ])
    rep = tax.build_tax_report(d).to_dict()
    for key in ("year", "available_years", "summary", "wash_sales",
                "wash_sale_count", "total_disallowed_loss", "lots"):
        assert key in rep
