"""Tests for dividend tracking & total-return accounting (P1-6)."""

from __future__ import annotations

from datetime import date

from analytics.dividends import (
    dividend_adjusted_price,
    dividends_between,
    fetch_dividends,
    is_dividend_gap,
    monthly_dividend_income,
    portfolio_dividend_income,
    position_dividend_income,
    recent_dividend,
    total_return,
    upcoming_ex_dividends,
    clear_cache,
)


_DIVS = [
    {"ex_date": "2024-02-09", "amount": 0.24},
    {"ex_date": "2024-05-10", "amount": 0.25},
    {"ex_date": "2024-08-12", "amount": 0.25},
]


class TestFetchNormalize:
    def test_fetch_with_injected_fetcher(self) -> None:
        clear_cache()

        def fake(symbol):
            return {date(2024, 2, 9): 0.24, date(2024, 5, 10): 0.25}

        recs = fetch_dividends("AAPL", fetcher=fake)
        assert len(recs) == 2
        assert recs[0]["ex_date"] == "2024-02-09"
        assert recs[0]["amount"] == 0.24

    def test_fetch_from_series_like(self) -> None:
        clear_cache()

        class _Series:
            def items(self):
                return [(date(2024, 2, 9), 0.24)]

        recs = fetch_dividends("MSFT", fetcher=lambda s: _Series())
        assert recs == [{"ex_date": "2024-02-09", "amount": 0.24}]

    def test_bad_data_dropped(self) -> None:
        clear_cache()
        recs = fetch_dividends(
            "X", fetcher=lambda s: [("2024-01-01", "bad"), (None, 0.5),
                                    ("2024-03-01", -1)]
        )
        assert recs == []


class TestIncome:
    def test_dividends_between_exclusive_start(self) -> None:
        # Entry on the 05-10 ex-date does NOT accrue that dividend.
        out = dividends_between(_DIVS, "2024-05-10", "2024-12-31")
        assert [r["ex_date"] for r in out] == ["2024-08-12"]

    def test_position_income(self) -> None:
        income = position_dividend_income(
            100, entry_date="2024-01-01", dividends=_DIVS, as_of="2024-06-01"
        )
        # 0.24 + 0.25 = 0.49 * 100 shares.
        assert income == 49.0

    def test_portfolio_income(self) -> None:
        positions = [
            {"symbol": "AAPL", "quantity": 100, "entry_time": "2024-01-01"},
            {"symbol": "NODIV", "quantity": 50, "entry_time": "2024-01-01"},
        ]
        out = portfolio_dividend_income(
            positions, {"AAPL": _DIVS, "NODIV": []}, as_of="2024-12-31"
        )
        assert out["total"] == 74.0  # (0.24+0.25+0.25)*100
        assert out["by_position"][0]["symbol"] == "AAPL"

    def test_total_return(self) -> None:
        tr = total_return(1000.0, 74.0)
        assert tr["total_return"] == 1074.0


class TestGapHandling:
    def test_is_dividend_gap_true(self) -> None:
        # Dropped ~0.25 on a 0.25 dividend -> gap.
        assert is_dividend_gap(100.0, 99.75, 0.25) is True

    def test_is_dividend_gap_false_on_big_drop(self) -> None:
        # Dropped 5.00 on a 0.25 dividend -> real breakdown, not a dividend gap.
        assert is_dividend_gap(100.0, 95.0, 0.25) is False

    def test_is_dividend_gap_no_dividend(self) -> None:
        assert is_dividend_gap(100.0, 99.0, 0.0) is False

    def test_recent_dividend_window(self) -> None:
        divs = [{"ex_date": "2024-05-10", "amount": 0.25}]
        assert recent_dividend(divs, as_of="2024-05-11", window_days=3) == 0.25
        assert recent_dividend(divs, as_of="2024-05-20", window_days=3) == 0.0

    def test_dividend_adjusted_price_adds_back(self) -> None:
        divs = [{"ex_date": "2024-05-10", "amount": 0.25}]
        adj = dividend_adjusted_price(99.75, divs, as_of="2024-05-10")
        assert adj == 100.0


class TestUpcomingAndMonthly:
    _POS = [{"symbol": "AAPL", "quantity": 10, "entry_time": "2026-01-01", "cost_basis": 1000.0}]
    _DIVS = {
        "AAPL": [
            {"ex_date": "2025-11-10", "amount": 0.24},   # before entry -> excluded
            {"ex_date": "2026-02-10", "amount": 0.25},   # accrued
            {"ex_date": "2026-08-10", "amount": 0.30},   # upcoming
        ]
    }

    def test_upcoming_only_future_ex_dates(self) -> None:
        rows = upcoming_ex_dividends(self._POS, self._DIVS, as_of="2026-07-20")
        assert len(rows) == 1
        r = rows[0]
        assert r["symbol"] == "AAPL"
        assert r["ex_date"] == "2026-08-10"
        assert r["est_payment"] == 3.0  # 10 shares * 0.30

    def test_upcoming_respects_limit(self) -> None:
        rows = upcoming_ex_dividends(self._POS, self._DIVS, as_of="2020-01-01", limit=2)
        assert len(rows) == 2  # three future dates, capped at 2

    def test_monthly_buckets_income_after_entry(self) -> None:
        months = monthly_dividend_income(self._POS, self._DIVS, months=12, as_of="2026-07-20")
        assert len(months) == 12
        by_month = {m["month"]: m["income"] for m in months}
        assert by_month["2026-02"] == 2.5   # 10 * 0.25
        # Pre-entry Nov 2025 dividend does not accrue.
        assert by_month.get("2025-11", 0.0) == 0.0

    def test_monthly_ends_at_as_of_month(self) -> None:
        months = monthly_dividend_income(self._POS, self._DIVS, months=6, as_of="2026-07-20")
        assert months[-1]["month"] == "2026-07"
        assert months[0]["month"] == "2026-02"
