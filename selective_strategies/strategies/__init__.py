"""
Highly selective strategy registry.

Each strategy is a pluggable detector function with the same signature.
The scanner iterates STRATEGY_PRIORITY and returns the first qualifying
signal per symbol.
"""
from __future__ import annotations
from typing import Callable, Dict, List, Optional
import pandas as pd
from selective_strategies.signal import SelectiveSignal
from selective_strategies.strategies import (
    rsi2_reversal,
    triple_timeframe,
    bb_climax,
    pead_drift,
    gap_fill,
    turnaround_tuesday,
)

STRATEGY_PRIORITY: List[str] = [
    "hs_rsi2_reversal",
    "hs_triple_timeframe",
    "hs_bb_climax",
    "hs_pead_drift",
    "hs_gap_fill",
    "hs_turnaround_tuesday",
]

DETECTORS: Dict[str, Callable[..., Optional[SelectiveSignal]]] = {
    "hs_rsi2_reversal": rsi2_reversal.detect,
    "hs_triple_timeframe": triple_timeframe.detect,
    "hs_bb_climax": bb_climax.detect,
    "hs_pead_drift": pead_drift.detect,
    "hs_gap_fill": gap_fill.detect,
    "hs_turnaround_tuesday": turnaround_tuesday.detect,
}
