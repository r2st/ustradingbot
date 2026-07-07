"""
Short-strategy registry.

``STRATEGY_PRIORITY`` orders the detectors per spec section 6: event-driven
setups first (their edge decays fastest), trend continuation next, and the
slower relative-weakness ranks last.  The scanner tries them in order per
symbol; the first qualifying signal wins (mirroring the long screener).

``short_adx_filter`` is not in the priority list — it acts as a confirmation
filter over the trend-following setups (see ``scanner.py``); its standalone
detector mode is off by default.
"""

from __future__ import annotations

from typing import Callable, Dict, List

from short_strategies.strategies import (
    adx_filter,
    bear_flag,
    buying_climax,
    earnings_pop_fade,
    gap_fail,
    laggard_fade,
    ma_crossunder,
    overbought_fade,
    relative_weakness,
    support_breakdown,
    vwap_rejection,
)

#: Detectors that continue an established downtrend; these are the ones the
#: ADX confirmation filter (strategy 4) gates when enabled.
TREND_FOLLOWING: frozenset = frozenset(
    {
        "short_support_breakdown",
        "short_ma_crossunder",
        "short_bear_flag",
        "short_relative_weakness",
        "short_laggard_fade",
    }
)

#: Priority order — first qualifying signal wins per symbol.
STRATEGY_PRIORITY: List[str] = [
    "short_gap_fail",
    "short_earnings_pop_fade",
    "short_support_breakdown",
    "short_bear_flag",
    "short_buying_climax",
    "short_overbought_fade",
    "short_vwap_rejection",
    "short_ma_crossunder",
    "short_relative_weakness",
    "short_laggard_fade",
]

DETECTORS: Dict[str, Callable] = {
    "short_support_breakdown": support_breakdown.detect,
    "short_ma_crossunder": ma_crossunder.detect,
    "short_bear_flag": bear_flag.detect,
    "short_adx_filter": adx_filter.detect,
    "short_relative_weakness": relative_weakness.detect,
    "short_laggard_fade": laggard_fade.detect,
    "short_overbought_fade": overbought_fade.detect,
    "short_gap_fail": gap_fail.detect,
    "short_earnings_pop_fade": earnings_pop_fade.detect,
    "short_vwap_rejection": vwap_rejection.detect,
    "short_buying_climax": buying_climax.detect,
}
