"""
ATR-based stop placement and position sizing for short trades (spec 7.4/7.5).

Engine-mode sizing ultimately runs through ``RiskManager.build_order`` (which
is direction-aware and applies the short risk modifier); the functions here
are the standalone maths used by the detectors (stop/target placement), the
backtest runner, and anyone sizing outside the engine.
"""

from __future__ import annotations

from typing import Optional, Tuple

from short_strategies.common.config import SharedFilterConfig


def short_stop_target(
    entry: float,
    atr_value: float,
    config: SharedFilterConfig,
    structural_stop: Optional[float] = None,
) -> Tuple[float, float]:
    """Return ``(stop, target)`` for a short entered at *entry*.

    The stop is a buy-stop ``stop_atr_mult x ATR`` above the entry.  A
    detector-supplied *structural_stop* (e.g. just above a flag high or a
    resistance level) is preferred when it is tighter than the ATR stop but
    still above the entry, and is capped at ``max_stop_atr_mult x ATR`` so a
    distant structural level can never blow out the risk per share.

    The target covers at ``target_rr`` times the final risk below the entry,
    floored at 1% of entry so a degenerate target can never go non-positive.
    """
    atr_stop = entry + config.stop_atr_mult * atr_value
    stop = atr_stop
    if structural_stop is not None and structural_stop > entry:
        cap = entry + config.max_stop_atr_mult * atr_value
        stop = min(max(structural_stop, entry + 0.25 * atr_value), cap)
    risk = stop - entry
    target = max(entry - config.target_rr * risk, entry * 0.01)
    return round(stop, 4), round(target, 4)


def size_short_position(
    account_equity: float,
    risk_pct: float,
    entry: float,
    stop: float,
) -> int:
    """Shares to short so that a stop-out loses ``risk_pct`` of equity.

    Args:
        account_equity: Total account value in the trade's currency.
        risk_pct: Fraction of equity to risk (spec: 0.005-0.01).
        entry: Short-sale price.
        stop: Buy-stop price (must be above entry).

    Returns:
        Whole share count (0 when the inputs are degenerate).
    """
    risk_per_share = stop - entry
    if risk_per_share <= 0 or entry <= 0 or account_equity <= 0 or risk_pct <= 0:
        return 0
    return int((account_equity * risk_pct) / risk_per_share)
