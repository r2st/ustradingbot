"""Tests for tax-loss harvesting + IRS 8949 / Schedule D export (P1-7)."""

from __future__ import annotations

from analytics.tax import RealizedLot
from analytics.tax_harvest import (
    form_8949_csv,
    form_8949_rows,
    scan_harvest_opportunities,
    schedule_d_summary,
)


def _lot(symbol, qty, cost, proceeds, term="short", wash=False, disallowed=0.0):
    gain = round(proceeds - cost, 2)
    return RealizedLot(
        symbol=symbol,
        quantity=qty,
        acquired="2024-01-15",
        disposed="2024-06-20",
        holding_days=157,
        term=term,
        cost_basis=cost,
        proceeds=proceeds,
        gain=gain,
        tax_year=2024,
        wash_sale=wash,
        disallowed_loss=disallowed,
    )


# ---------------------------------------------------------------------------
# Harvesting scanner
# ---------------------------------------------------------------------------


class TestHarvestScanner:
    def test_finds_losing_position(self) -> None:
        positions = [
            {"symbol": "AAPL", "quantity": 100, "entry_price": 200,
             "entry_time": "2024-01-01"},
            {"symbol": "MSFT", "quantity": 50, "entry_price": 300,
             "entry_time": "2024-01-01"},  # a winner, excluded
        ]
        prices = {"AAPL": 180.0, "MSFT": 350.0}
        opps = scan_harvest_opportunities(positions, prices, as_of="2024-06-01")
        assert len(opps) == 1
        assert opps[0].symbol == "AAPL"
        assert opps[0].unrealized_loss == 2000.0  # (200-180)*100

    def test_min_loss_filter(self) -> None:
        positions = [{"symbol": "AAPL", "quantity": 1, "entry_price": 200,
                      "entry_time": "2024-01-01"}]
        prices = {"AAPL": 190.0}  # $10 loss
        assert scan_harvest_opportunities(positions, prices, min_loss=50) == []

    def test_wash_sale_flagged(self) -> None:
        positions = [{"symbol": "AAPL", "quantity": 100, "entry_price": 200,
                      "entry_time": "2024-01-01"}]
        prices = {"AAPL": 180.0}
        # A recent buy 10 days before the (as_of) sale triggers wash-sale risk.
        opps = scan_harvest_opportunities(
            positions, prices,
            recent_buys_by_symbol={"AAPL": ["2024-05-22"]},
            as_of="2024-06-01",
        )
        assert opps[0].wash_sale_risk is True
        assert "disallowed" in opps[0].wash_sale_reason

    def test_no_wash_when_buy_old(self) -> None:
        positions = [{"symbol": "AAPL", "quantity": 100, "entry_price": 200,
                      "entry_time": "2024-01-01"}]
        opps = scan_harvest_opportunities(
            positions, {"AAPL": 180.0},
            recent_buys_by_symbol={"AAPL": ["2024-01-01"]},  # far in the past
            as_of="2024-06-01",
        )
        assert opps[0].wash_sale_risk is False
        assert opps[0].earliest_rebuy_date is not None

    def test_long_term_classification(self) -> None:
        positions = [{"symbol": "AAPL", "quantity": 10, "entry_price": 200,
                      "entry_time": "2023-01-01"}]
        opps = scan_harvest_opportunities(
            positions, {"AAPL": 150.0}, as_of="2024-06-01"
        )
        assert opps[0].term == "long"

    def test_shorts_skipped(self) -> None:
        positions = [{"symbol": "AAPL", "quantity": 10, "entry_price": 200,
                      "direction": "short", "entry_time": "2024-01-01"}]
        assert scan_harvest_opportunities(positions, {"AAPL": 150.0}) == []


# ---------------------------------------------------------------------------
# Form 8949
# ---------------------------------------------------------------------------


class TestForm8949:
    def test_rows_split_by_term(self) -> None:
        lots = [
            _lot("AAPL", 10, 2000, 1800, term="short"),
            _lot("MSFT", 5, 1500, 1800, term="long"),
        ]
        rows = form_8949_rows(lots)
        assert len(rows["short_term"]) == 1
        assert len(rows["long_term"]) == 1
        assert rows["short_term"][0]["gain_loss"] == -200.0

    def test_wash_sale_adjustment(self) -> None:
        lot = _lot("AAPL", 10, 2000, 1800, term="short", wash=True, disallowed=200.0)
        row = form_8949_rows([lot])["short_term"][0]
        assert row["code"] == "W"
        assert row["adjustment"] == 200.0
        # Loss of -200 fully disallowed -> gain/loss after adjustment == 0.
        assert row["gain_loss"] == 0.0

    def test_csv_has_both_parts_and_totals(self) -> None:
        lots = [
            _lot("AAPL", 10, 2000, 1800, term="short"),
            _lot("MSFT", 5, 1500, 1800, term="long"),
        ]
        csv_text = form_8949_csv(lots)
        assert "Part I — Short-Term" in csv_text
        assert "Part II — Long-Term" in csv_text
        assert "Totals" in csv_text
        assert "AAPL" in csv_text and "MSFT" in csv_text


# ---------------------------------------------------------------------------
# Schedule D
# ---------------------------------------------------------------------------


class TestScheduleD:
    def test_summary_totals(self) -> None:
        lots = [
            _lot("AAPL", 10, 2000, 1800, term="short"),   # -200
            _lot("MSFT", 5, 1500, 1800, term="long"),     # +300
        ]
        d = schedule_d_summary(lots)
        assert d["short_term"]["gain_loss"] == -200.0
        assert d["long_term"]["gain_loss"] == 300.0
        assert d["net_gain_loss"] == 100.0

    def test_wash_sale_adjustment_in_summary(self) -> None:
        lots = [_lot("AAPL", 10, 2000, 1800, term="short", wash=True, disallowed=200.0)]
        d = schedule_d_summary(lots)
        assert d["short_term"]["adjustments"] == 200.0
        assert d["short_term"]["gain_loss"] == 0.0  # loss disallowed
