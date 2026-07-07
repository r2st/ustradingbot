"""
Standardized short-signal object emitted by every short strategy.

``ShortSignal`` is the module-internal representation defined by the spec
(section 2.1).  ``to_core_signal()`` converts it to the pipeline-native
``signals.signal_types.Signal`` (``direction="short"``) so the engine, risk
manager, AI veto, and journal consume short signals with zero special-casing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List

from signals.signal_types import Grade, Signal


@dataclass
class ShortSignal:
    """A standardized short-entry signal.

    Attributes:
        strategy_id: Emitting strategy (e.g. ``"short_gap_fail"``).  Always
            prefixed ``short_`` — the risk manager's strategy-family cap and
            sizing modifier key off this prefix.
        symbol: Ticker symbol.
        signal_strength: Composite quality score in [0.0, 1.0].
        trigger_price: Proposed short-sale price (typically the last close).
        stop_price: Buy-stop above the entry.
        target_price: Cover target below the entry.
        side: Always ``"SHORT"`` (spec-mandated field).
        timestamp: Signal generation time.
        filters_passed: Names of the shared risk filters that approved the
            signal (populated by the scanner's filter chain).
        metadata: Strategy-specific diagnostics (pattern measurements etc.).
    """

    strategy_id: str
    symbol: str
    signal_strength: float
    trigger_price: float
    stop_price: float
    target_price: float
    side: str = "SHORT"
    timestamp: datetime = field(default_factory=datetime.now)
    filters_passed: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def risk_per_share(self) -> float:
        """Dollar risk per share (stop above entry for a short)."""
        return self.stop_price - self.trigger_price

    @property
    def reward_per_share(self) -> float:
        """Dollar reward per share (target below entry for a short)."""
        return self.trigger_price - self.target_price

    def is_price_valid(self) -> bool:
        """Whether the price levels describe a structurally valid short."""
        return (
            self.trigger_price > 0
            and self.stop_price > self.trigger_price
            and 0 < self.target_price < self.trigger_price
        )

    def to_core_signal(self) -> Signal:
        """Convert to the pipeline-native :class:`Signal`.

        The grade derives from ``signal_strength`` via the shared thresholds;
        the spec-specific fields (``filters_passed``, ``metadata``, ``side``)
        travel in ``raw_data`` so the journal and rationale capture them.
        """
        strength = max(0.0, min(1.0, float(self.signal_strength)))
        return Signal(
            symbol=self.symbol,
            strategy=self.strategy_id,
            direction="short",
            entry_price=round(self.trigger_price, 4),
            stop_price=round(self.stop_price, 4),
            target_price=round(self.target_price, 4),
            signal_strength=round(strength, 4),
            grade=Grade.from_score(strength),
            raw_data={
                "side": self.side,
                "filters_passed": list(self.filters_passed),
                "metadata": dict(self.metadata),
            },
            timestamp=self.timestamp,
        )
