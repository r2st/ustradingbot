"""
Universal signal and trade data types for the US Trading Bot.

This module defines the core dataclasses used across the entire trading
pipeline: signal detection, order building, and exit management. Every
module that produces or consumes trade signals speaks this common language.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ExitReason(str, Enum):
    """Why a position was closed.

    Each variant maps to a specific exit path in the exit manager.
    The string values are stored in trades.csv and open_positions.json,
    so they must remain stable across releases.
    """

    STOP_HIT = "STOP_HIT"
    TARGET_HIT = "TARGET_HIT"
    TIME_EXIT_LOSS = "TIME_EXIT_LOSS"
    TIME_EXIT_FLAT = "TIME_EXIT_FLAT"
    TIME_EXIT_ZOMBIE = "TIME_EXIT_ZOMBIE"
    SETUP_BROKEN = "SETUP_BROKEN"
    PARTIAL_TAKE = "PARTIAL_TAKE"
    PHANTOM_NEVER_OPENED = "PHANTOM_NEVER_OPENED"
    MANUAL = "MANUAL"


class Grade(str, Enum):
    """Signal quality grade derived from the combined weighted score.

    Thresholds (from ``SYSTEM_DESIGN.md`` section 5.4):
        A >= 0.78  -- Trade at 100% size
        B >= 0.65  -- Trade at 75% size
        C >= 0.38  -- Skip (insufficient quality)
        F <  0.38  -- Block (hard veto or very weak)
    """

    A = "A"
    B = "B"
    C = "C"
    F = "F"

    @classmethod
    def from_score(cls, score: float) -> "Grade":
        """Convert a numeric score (0.0-1.0) to its corresponding grade.

        Args:
            score: Combined weighted signal score, clamped to [0.0, 1.0].

        Returns:
            The appropriate ``Grade`` for the given score.

        Examples:
            >>> Grade.from_score(0.85)
            <Grade.A: 'A'>
            >>> Grade.from_score(0.70)
            <Grade.B: 'B'>
            >>> Grade.from_score(0.50)
            <Grade.C: 'C'>
            >>> Grade.from_score(0.20)
            <Grade.F: 'F'>
        """
        if score >= 0.78:
            return cls.A
        if score >= 0.65:
            return cls.B
        if score >= 0.38:
            return cls.C
        return cls.F


# ---------------------------------------------------------------------------
# Signal -- universal output of the scoring engine
# ---------------------------------------------------------------------------


@dataclass
class Signal:
    """A scored trade signal produced by the screener / combined filter.

    Every strategy (VCP, PEAD, Momentum, Swing, Mean Reversion) emits
    signals in this format.  Downstream consumers -- the AI veto layer,
    risk manager, and order builder -- all operate on ``Signal`` instances.

    Attributes:
        symbol: Ticker symbol (e.g. ``"AAPL"``, ``"SHOP.TO"``).
        strategy: Strategy that generated this signal
            (``"momentum"``, ``"swing"``, ``"vcp_breakout"``,
            ``"pead"``, ``"mean_reversion"``).
        direction: Trade direction: ``"long"`` (default) or ``"short"``
            (emitted by the ``short_strategies`` module).
        entry_price: Proposed entry price (limit buy, or short-sale price).
        stop_price: Initial stop-loss price (below entry for longs, above
            entry for shorts).
        target_price: Take-profit target (above entry for longs, below for
            shorts).
        signal_strength: Combined weighted score in [0.0, 1.0].
        grade: Quality grade derived from ``signal_strength``.
        rsi_value: Raw RSI(14) value at signal time.
        rsi_score: RSI bullish score in [0.0, 1.0].
        macd_histogram: MACD histogram value at signal time.
        macd_score: MACD bullish score in [0.0, 1.0].
        ema_score: EMA structure score in [0.0, 1.0].
        volume_ratio: Today's volume / 20-day average volume.
        volume_score: Volume bullish score in [0.0, 1.0].
        ripster_score: Ripster EMA cloud score in [0.0, 1.0].
        obv_confirming: Whether OBV trend confirms price trend.
        raw_data: Arbitrary extra data attached by the strategy module.
        timestamp: When the signal was generated (defaults to now).
    """

    # --- identifiers -------------------------------------------------------
    symbol: str
    strategy: str
    direction: str = "long"

    # --- price levels ------------------------------------------------------
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0

    # --- composite score ---------------------------------------------------
    signal_strength: float = 0.0
    grade: Grade = Grade.F

    # --- individual indicator values / scores ------------------------------
    rsi_value: float = 0.0
    rsi_score: float = 0.0
    macd_histogram: float = 0.0
    macd_score: float = 0.0
    ema_score: float = 0.0
    volume_ratio: float = 0.0
    volume_score: float = 0.0
    ripster_score: float = 0.0
    obv_confirming: bool = False

    # --- metadata ----------------------------------------------------------
    raw_data: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    # --- derived helpers ---------------------------------------------------

    @property
    def is_short(self) -> bool:
        """Whether this is a short-side signal (``direction="short"``)."""
        return self.direction.lower() == "short"

    @property
    def risk_per_share(self) -> float:
        """Dollar risk per share, direction-aware.

        Long: entry minus stop (stop below entry).
        Short: stop minus entry (buy-stop above entry).

        Returns:
            Positive float for a structurally valid setup, otherwise a
            non-positive value indicating an invalid one.
        """
        if self.is_short:
            return self.stop_price - self.entry_price
        return self.entry_price - self.stop_price

    @property
    def reward_per_share(self) -> float:
        """Dollar reward per share, direction-aware.

        Long: target minus entry.  Short: entry minus target (cover below).
        """
        if self.is_short:
            return self.entry_price - self.target_price
        return self.target_price - self.entry_price

    @property
    def risk_reward_ratio(self) -> float:
        """Reward-to-risk ratio (R:R).

        Returns:
            ``reward / risk`` when risk > 0, otherwise ``0.0``.
            A value of 2.0 means the target is 2x the stop distance.
        """
        risk = self.risk_per_share
        if risk <= 0:
            return 0.0
        return self.reward_per_share / risk


# ---------------------------------------------------------------------------
# TradeOrder -- a sized, AI-approved order ready for execution
# ---------------------------------------------------------------------------


@dataclass
class TradeOrder:
    """A fully qualified trade order ready for bracket-order placement.

    Built by the risk manager after position sizing, and enriched with
    the AI layer's decision and cost tracking.

    Attributes:
        signal: The underlying ``Signal`` that spawned this order.
        quantity: Number of shares to buy (0 means skip).
        risk_amount: Total dollar risk = ``risk_per_share * quantity``.
        max_risk_dollars: The per-trade risk budget that was available.
        currency: ISO currency code (``"USD"`` or ``"CAD"``).
        ai_decision: AI veto layer outcome (``"APPROVE"`` or ``"REJECT"``).
        ai_reasoning: Free-text explanation from the AI layer.
        ai_cost_usd: Estimated API cost for the AI evaluation in USD.
    """

    signal: Signal
    quantity: int = 0
    risk_amount: float = 0.0
    max_risk_dollars: float = 0.0
    currency: str = "USD"
    ai_decision: str = ""
    ai_reasoning: str = ""
    ai_cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# ExitEvent -- record of a position closure
# ---------------------------------------------------------------------------


@dataclass
class ExitEvent:
    """Record of a position exit, used by the exit manager and journal.

    Attributes:
        symbol: Ticker symbol of the closed position.
        exit_price: Price at which the position was exited.
        exit_reason: Why the position was closed.
        exit_date: Timestamp of the exit.
        pnl_gross: Gross P&L before commissions = ``(exit - entry) * qty``.
        fill_details: Broker-level fill information (order IDs, partial
            fills, commissions) stored as a free-form dict.
    """

    symbol: str
    exit_price: float = 0.0
    exit_reason: ExitReason = ExitReason.MANUAL
    exit_date: Optional[datetime] = None
    pnl_gross: float = 0.0
    fill_details: Dict[str, Any] = field(default_factory=dict)
