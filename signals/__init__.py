"""
Signal scoring engine for the US Trading Bot.

Public API
----------
.. autofunction:: combined_filter.score_symbol
.. autoclass:: signal_types.Signal
.. autoclass:: signal_types.Grade

Indicator modules (each self-contained):
    - :mod:`rsi_signals`
    - :mod:`macd_signals`
    - :mod:`ema_signals`
    - :mod:`volume_signals`
    - :mod:`ripster_cloud`
"""

from signals.combined_filter import score_symbol
from signals.screener import run_full_scan
from signals.signal_types import Grade, Signal

__all__ = [
    "Grade",
    "Signal",
    "run_full_scan",
    "score_symbol",
]
