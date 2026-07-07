"""Unit tests for the shared risk filter chain (spec section 7)."""

from __future__ import annotations

import json
from datetime import date, timedelta

from short_strategies.common.config import SharedFilterConfig
from short_strategies.common.signal import ShortSignal
from short_strategies.risk.filters import ShortFilterChain, StaticBorrowProvider


def _sig(symbol: str = "XYZ", entry: float = 100.0, stop: float = 103.0,
         target: float = 94.0, strength: float = 0.8) -> ShortSignal:
    return ShortSignal(
        strategy_id="short_gap_fail",
        symbol=symbol,
        signal_strength=strength,
        trigger_price=entry,
        stop_price=stop,
        target_price=target,
    )


def _chain(cfg: SharedFilterConfig | None = None, **kwargs) -> ShortFilterChain:
    """A chain with every external lookup stubbed benign by default."""
    defaults = dict(
        short_pct_float_fn=lambda s: 0.05,
        next_earnings_fn=lambda s: None,
        regime_ok_fn=lambda: (True, "bear"),
    )
    defaults.update(kwargs)
    return ShortFilterChain(
        cfg or SharedFilterConfig(), total_capital=12_000.0, **defaults
    )


class TestBorrowLocate:
    def test_default_allows(self):
        ok, reason = _chain().apply(_sig())
        assert ok, reason

    def test_deny_list_blocks(self, tmp_path):
        (tmp_path / "hard_to_borrow.json").write_text(json.dumps(["XYZ"]))
        chain = _chain(borrow_provider=StaticBorrowProvider(tmp_path))
        ok, reason = chain.apply(_sig("XYZ"))
        assert not ok and reason.startswith("borrow_unavailable")

    def test_provider_malformed_file_allows(self, tmp_path):
        (tmp_path / "hard_to_borrow.json").write_text("not json")
        provider = StaticBorrowProvider(tmp_path)
        assert provider.can_borrow("XYZ")


class TestShortInterest:
    def test_high_short_interest_blocked(self):
        chain = _chain(short_pct_float_fn=lambda s: 0.35)
        ok, reason = chain.apply(_sig())
        assert not ok and reason.startswith("short_interest_too_high")

    def test_missing_data_fail_open_default(self):
        chain = _chain(short_pct_float_fn=lambda s: None)
        ok, _ = chain.apply(_sig())
        assert ok

    def test_missing_data_fail_closed_when_configured(self):
        cfg = SharedFilterConfig(short_interest_fail_open=False)
        chain = _chain(cfg, short_pct_float_fn=lambda s: None)
        ok, reason = chain.apply(_sig())
        assert not ok and reason.startswith("short_interest_unknown")

    def test_zero_limit_disables(self):
        cfg = SharedFilterConfig(max_short_pct_float=0.0)
        chain = _chain(cfg, short_pct_float_fn=lambda s: 0.9)
        ok, _ = chain.apply(_sig())
        assert ok


class TestMarketRegime:
    def test_bull_regime_blocks(self):
        chain = _chain(regime_ok_fn=lambda: (False, "bull"))
        ok, reason = chain.apply(_sig())
        assert not ok and reason.startswith("regime_blocks_shorts")

    def test_disabled_filter_allows_bull(self):
        cfg = SharedFilterConfig(regime_filter_enabled=False)
        chain = _chain(cfg, regime_ok_fn=lambda: (False, "bull"))
        ok, _ = chain.apply(_sig())
        assert ok

    def test_regime_verdict_cached_per_scan(self):
        calls = []

        def regime():
            calls.append(1)
            return True, "bear"

        chain = _chain(regime_ok_fn=regime)
        chain.apply(_sig("A"))
        chain.apply(_sig("B"))
        assert len(calls) == 1


class TestEarningsBlackout:
    def test_upcoming_earnings_blocks(self):
        soon = date.today() + timedelta(days=1)
        chain = _chain(next_earnings_fn=lambda s: soon)
        ok, reason = chain.apply(_sig())
        assert not ok and reason.startswith("earnings_blackout")

    def test_distant_earnings_allowed(self):
        far = date.today() + timedelta(days=30)
        chain = _chain(next_earnings_fn=lambda s: far)
        ok, _ = chain.apply(_sig())
        assert ok

    def test_no_earnings_data_fails_open(self):
        chain = _chain(next_earnings_fn=lambda s: None)
        ok, _ = chain.apply(_sig())
        assert ok


class TestExposureCap:
    def test_open_shorts_count_against_cap(self):
        cfg = SharedFilterConfig(max_short_exposure_pct=0.25)  # $3k of $12k
        chain = _chain(cfg)
        open_positions = {
            "AAA": {"direction": "short", "entry_price": 100.0, "quantity": 28},
        }  # $2,800 open short notional
        ok, reason = chain.apply(_sig(), open_positions)
        assert not ok and reason.startswith("short_exposure_cap")

    def test_long_positions_do_not_count(self):
        cfg = SharedFilterConfig(max_short_exposure_pct=0.25)
        chain = _chain(cfg)
        open_positions = {
            "AAA": {"direction": "long", "entry_price": 100.0, "quantity": 100},
        }
        ok, _ = chain.apply(_sig(), open_positions)
        assert ok

    def test_accepted_notional_accumulates(self):
        cfg = SharedFilterConfig(max_short_exposure_pct=0.25)
        chain = _chain(cfg)
        sig = _sig()
        ok, _ = chain.apply(sig, {}, accepted_notional=0.0)
        assert ok
        est = chain.estimate_notional(sig)
        assert est > 0
        ok2, reason = chain.apply(_sig("OTHER"), {}, accepted_notional=3_000.0)
        assert not ok2 and reason.startswith("short_exposure_cap")


class TestChainAnnotation:
    def test_filters_passed_recorded(self):
        sig = _sig()
        ok, _ = _chain().apply(sig, {})
        assert ok
        assert sig.filters_passed == [
            "borrow_locate", "short_interest", "market_regime",
            "earnings_blackout", "exposure_cap",
        ]

    def test_invalid_price_levels_rejected(self):
        bad = _sig(stop=99.0)  # stop below entry: invalid for a short
        ok, reason = _chain().apply(bad)
        assert not ok and reason == "invalid_price_levels"
