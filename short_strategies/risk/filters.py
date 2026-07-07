"""
Shared risk/execution filters for short candidates (spec section 7).

The scanner runs every candidate through :class:`ShortFilterChain` before it
becomes a pipeline signal.  Each filter appends its name to the signal's
``filters_passed`` on success; the first failure rejects the candidate with a
diagnostic reason.  Data lookups (borrow list, short interest, earnings
calendar, regime) are injectable so every filter is unit-testable offline.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Protocol, Tuple

import structlog

from short_strategies.common.config import SharedFilterConfig
from short_strategies.common.signal import ShortSignal

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Borrow / locate (7.1)
# ---------------------------------------------------------------------------


class BorrowProvider(Protocol):
    """Answers "can this symbol be borrowed for a short sale?"."""

    def can_borrow(self, symbol: str) -> bool: ...


class StaticBorrowProvider:
    """Default borrow model: everything is borrowable except a deny-list.

    The scan universe is large-cap US/CA names that are easy to borrow in
    practice, so the default answer is *yes*; an operator can blacklist
    hard-to-borrow symbols in ``data_store/hard_to_borrow.json`` (a JSON
    array of tickers).  A real locate feed implements the same protocol.
    """

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self._deny: set[str] = set()
        if data_dir is not None:
            path = Path(data_dir) / "hard_to_borrow.json"
            try:
                if path.is_file():
                    items = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(items, list):
                        self._deny = {str(s).upper() for s in items}
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("borrow_provider.load_failed", error=str(exc))

    def can_borrow(self, symbol: str) -> bool:
        return symbol.upper() not in self._deny


# ---------------------------------------------------------------------------
# Short interest (7.2)
# ---------------------------------------------------------------------------


def default_short_pct_float(symbol: str) -> Optional[float]:
    """Short % of float via yfinance, or ``None`` when unavailable."""
    try:
        import yfinance as yf  # local import: keep module import cheap

        info = yf.Ticker(symbol).info or {}
        val = info.get("shortPercentOfFloat")
        return float(val) if val is not None else None
    except Exception:  # noqa: BLE001 -- any lookup failure means "unknown"
        return None


# ---------------------------------------------------------------------------
# Filter chain
# ---------------------------------------------------------------------------


class ShortFilterChain:
    """Applies the section-7 pre-entry filters to a :class:`ShortSignal`.

    Args:
        config: Shared filter parameters.
        total_capital: Account capital for the exposure-cap maths.
        borrow_provider: Locate source (default :class:`StaticBorrowProvider`).
        short_pct_float_fn: Short-interest lookup (default yfinance).
        next_earnings_fn: Upcoming-earnings lookup (default
            ``data.earnings_calendar.next_earnings_date``).
        regime_ok_fn: Callable returning whether the market regime permits
            shorts (default derives from ``analytics.regime.current_regime``).
    """

    def __init__(
        self,
        config: SharedFilterConfig,
        total_capital: float,
        borrow_provider: Optional[BorrowProvider] = None,
        short_pct_float_fn: Optional[Callable[[str], Optional[float]]] = None,
        next_earnings_fn: Optional[Callable[[str], Optional[date]]] = None,
        regime_ok_fn: Optional[Callable[[], Tuple[bool, str]]] = None,
        data_dir: Optional[Path] = None,
        max_position_size_pct: float = 0.015,
        risk_modifier: float = 0.65,
    ) -> None:
        self._cfg = config
        self._capital = float(total_capital)
        # Per-trade risk budget fraction, mirroring RiskManager.build_order's
        # sizing so the exposure cap charges a realistic notional estimate.
        self._risk_budget_frac = max_position_size_pct * risk_modifier
        self._borrow = borrow_provider or StaticBorrowProvider(data_dir)
        self._short_pct = short_pct_float_fn or default_short_pct_float
        self._next_earnings = next_earnings_fn
        self._regime_ok = regime_ok_fn
        # The regime verdict is per-scan, not per-symbol: cache it.
        self._regime_cache: Optional[Tuple[bool, str]] = None

    # ------------------------------------------------------------- filters

    def _check_borrow(self, sig: ShortSignal) -> Tuple[bool, str]:
        if not self._borrow.can_borrow(sig.symbol):
            return False, f"borrow_unavailable:{sig.symbol}"
        return True, "borrow_locate"

    def _check_short_interest(self, sig: ShortSignal) -> Tuple[bool, str]:
        limit = self._cfg.max_short_pct_float
        if limit <= 0:
            return True, "short_interest"
        pct = self._short_pct(sig.symbol)
        if pct is None:
            if self._cfg.short_interest_fail_open:
                return True, "short_interest"  # data missing -> documented fail-open
            return False, f"short_interest_unknown:{sig.symbol}"
        if pct > limit:
            return False, f"short_interest_too_high:{pct:.3f}>{limit:.3f}"
        return True, "short_interest"

    def _check_regime(self, sig: ShortSignal) -> Tuple[bool, str]:
        if not self._cfg.regime_filter_enabled:
            return True, "market_regime"
        if self._regime_cache is None:
            self._regime_cache = self._compute_regime_ok()
        ok, why = self._regime_cache
        if not ok:
            return False, f"regime_blocks_shorts:{why}"
        return True, "market_regime"

    def _compute_regime_ok(self) -> Tuple[bool, str]:
        """Shorts allowed when the benchmark is NOT in a bull regime.

        With ``regime_require_bear`` set, a strict bear regime is required
        (sideways also blocks).  Fails open to "sideways" semantics when the
        regime engine cannot produce a verdict, matching its own neutral
        fallback.
        """
        if self._regime_ok is not None:
            return self._regime_ok()
        try:
            from analytics.regime import current_regime
            from config.settings import get_settings

            result = current_regime(get_settings())
            regime = result.regime
        except Exception:  # noqa: BLE001 -- treat an engine failure as neutral
            regime, result = "sideways", None
        if regime == "bull":
            return False, "bull"
        if self._cfg.regime_require_bear and regime != "bear":
            return False, regime
        return True, regime

    def _check_earnings_blackout(self, sig: ShortSignal) -> Tuple[bool, str]:
        days = self._cfg.earnings_blackout_days
        if days <= 0:
            return True, "earnings_blackout"
        fn = self._next_earnings
        if fn is None:
            from data.earnings_calendar import next_earnings_date as fn  # type: ignore
        try:
            nxt = fn(sig.symbol)
        except Exception:  # noqa: BLE001 -- unknown calendar -> fail open
            nxt = None
        if nxt is not None:
            delta = (nxt - date.today()).days
            if 0 <= delta <= days:
                return False, f"earnings_blackout:{nxt.isoformat()}_in_{delta}d"
        return True, "earnings_blackout"

    def _check_exposure_cap(
        self,
        sig: ShortSignal,
        open_positions: Dict[str, Dict[str, Any]],
        accepted_notional: float,
    ) -> Tuple[bool, str]:
        cap = self._cfg.max_short_exposure_pct * self._capital
        open_short_notional = sum(
            float(p.get("entry_price", 0) or 0) * int(p.get("quantity", 0) or 0)
            for p in open_positions.values()
            if str(p.get("direction", "long")).lower() == "short"
        )
        # Estimate the candidate's notional from the per-trade risk budget:
        # shares ~= risk_dollars / risk_per_share (mirrors build_order).
        risk_ps = sig.risk_per_share
        if risk_ps <= 0:
            return False, "invalid_risk_per_share"
        total = open_short_notional + accepted_notional + self.estimate_notional(sig)
        if total > cap:
            return False, (
                f"short_exposure_cap:{total:.0f}>{cap:.0f}"
            )
        return True, "exposure_cap"

    # -------------------------------------------------------------- public

    def estimate_notional(self, sig: ShortSignal) -> float:
        """The notional estimate the exposure cap charges for *sig*."""
        risk_ps = sig.risk_per_share
        if risk_ps <= 0:
            return 0.0
        est_shares = int((self._capital * self._risk_budget_frac) / risk_ps)
        # Same 10%-of-capital notional cap that build_order applies.
        return min(est_shares * sig.trigger_price, 0.10 * self._capital)

    def apply(
        self,
        sig: ShortSignal,
        open_positions: Optional[Dict[str, Dict[str, Any]]] = None,
        accepted_notional: float = 0.0,
    ) -> Tuple[bool, str]:
        """Run all filters; annotate ``filters_passed`` on success.

        Args:
            sig: The candidate signal (mutated: ``filters_passed`` filled in).
            open_positions: Current open positions (risk-manager shape) for
                the exposure cap; ``None`` skips that filter.
            accepted_notional: Short notional already accepted earlier in
                this same scan (strongest-first ordering).

        Returns:
            ``(True, "passed")`` or ``(False, reason)``.
        """
        if not sig.is_price_valid():
            return False, "invalid_price_levels"

        checks = [
            self._check_borrow,
            self._check_short_interest,
            self._check_regime,
            self._check_earnings_blackout,
        ]
        passed: list[str] = []
        for check in checks:
            ok, label = check(sig)
            if not ok:
                log.info("short_filter.rejected", symbol=sig.symbol,
                         strategy=sig.strategy_id, reason=label)
                return False, label
            passed.append(label)

        if open_positions is not None:
            ok, label = self._check_exposure_cap(
                sig, open_positions, accepted_notional
            )
            if not ok:
                log.info("short_filter.rejected", symbol=sig.symbol,
                         strategy=sig.strategy_id, reason=label)
                return False, label
            passed.append(label)

        sig.filters_passed.extend(passed)
        return True, "passed"
