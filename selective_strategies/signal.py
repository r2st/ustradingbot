"""Signal type for highly selective strategies."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Any, Dict, List, Optional

from config.settings import EASTERN
from signals.signal_types import Grade, Signal


@dataclass
class SelectiveSignal:
    """Internal signal produced by a selective strategy detector.

    Converted to the pipeline-native Signal via to_core_signal() before
    entering the engine's entry pipeline.
    """

    strategy_id: str
    symbol: str
    signal_strength: float
    trigger_price: float
    stop_price: float
    target_price: float
    direction: str = "long"
    timestamp: datetime = field(default_factory=partial(datetime.now, tz=EASTERN))
    filters_passed: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def risk_per_share(self) -> float:
        if self.direction == "short":
            return self.stop_price - self.trigger_price
        return self.trigger_price - self.stop_price

    @property
    def grade(self) -> Grade:
        return Grade.from_score(self.signal_strength)

    def is_price_valid(self) -> bool:
        if self.trigger_price <= 0:
            return False
        if self.direction == "short":
            return self.stop_price > self.trigger_price and self.target_price < self.trigger_price
        return self.stop_price < self.trigger_price and self.target_price > self.trigger_price

    def to_core_signal(self) -> Signal:
        """Convert to the pipeline-native Signal for the engine."""
        return Signal(
            symbol=self.symbol,
            strategy=self.strategy_id,
            direction=self.direction,
            entry_price=round(self.trigger_price, 4),
            stop_price=round(self.stop_price, 4),
            target_price=round(self.target_price, 4),
            signal_strength=round(self.signal_strength, 4),
            grade=self.grade,
            raw_data={
                "selective_filters": self.filters_passed,
                "selective_metadata": self.metadata,
                "side": self.direction.upper(),
            },
            timestamp=self.timestamp,
        )
