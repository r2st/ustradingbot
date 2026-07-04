"""
Exit manager -- reconciles the broker with the journal and runs exit rules.

Every trading cycle begins with exit management (before any new entries) so
that freed capital is available for fresh signals and no stale position lingers
unreconciled.  This module implements the four exit paths from the design doc:

1. **Broker-initiated exits** -- bracket stop/target legs that have filled.
2. **Smart time-based exits** -- tiered handling of positions past their max
   hold period (zombie / losing-flat / moderate-profit / strong-runner).
3. **Position-health exits** -- re-score each open position; close it as
   ``SETUP_BROKEN`` when the thesis has collapsed and price is halfway to the
   stop.
4. **Trailing stops** -- ratchet the stop upward on winners using 2x ATR(14).

The manager coordinates three collaborators: the :class:`Broker` (reality),
the :class:`RiskManager` (intent + persisted position state), and the
:class:`TradeLogger` (the CSV journal).  Each closed position is journaled,
removed from the risk manager, and its P&L recorded against the daily limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd
import structlog

from config.settings import Settings
from data.fetcher import fetch_current_price, fetch_ohlcv
from execution.broker import Broker
from journal.trade_logger import TradeLogger
from risk.manager import RiskManager
from signals.combined_filter import score_symbol
from signals.signal_types import ExitEvent, ExitReason

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Per-strategy maximum hold (calendar days).  Falls back to HOLD_MAX_DAYS.
_MAX_HOLD_DAYS: Dict[str, int] = {
    "momentum": 20,
    "vcp_breakout": 25,
    "swing": 15,
    "pead": 30,
    "mean_reversion": 7,
}

# Position-health thresholds.
_HEALTH_SCORE_FLOOR = 0.35
_HEALTH_STOP_PROGRESS = 0.50  # price has moved >= 50% toward the stop


@dataclass
class ExitSummary:
    """Counts of what each exit sweep did, for cycle logging."""

    broker_exits: int = 0
    time_exits: int = 0
    health_exits: int = 0
    trails_updated: int = 0


class ExitManager:
    """Runs all exit logic for one cycle.

    Args:
        settings: Application settings.
        broker: The active broker (source of truth for fills).
        risk_manager: Position/intent tracker + daily P&L.
        trade_logger: CSV journal.
    """

    def __init__(
        self,
        settings: Settings,
        broker: Broker,
        risk_manager: RiskManager,
        trade_logger: TradeLogger,
    ) -> None:
        self._settings = settings
        self._broker = broker
        self._risk = risk_manager
        self._journal = trade_logger
        self._log = log.bind(component="ExitManager")

    # ------------------------------------------------------------- entry pt

    def manage_exits(self) -> ExitSummary:
        """Run all four exit checks in order and return a summary."""
        summary = ExitSummary()
        summary.broker_exits = self._check_broker_exits()
        summary.time_exits = self._check_time_based_exits()
        summary.health_exits = self._check_position_health()
        summary.trails_updated = self._check_trailing_stops()
        return summary

    # --------------------------------------------------- 1. broker exits

    def _check_broker_exits(self) -> int:
        """Reconcile bracket legs that filled at the broker."""
        count = 0
        for event in self._broker.poll_exits():
            self._finalise_exit(event)
            count += 1
        return count

    # --------------------------------------------------- 2. time-based exits

    def _check_time_based_exits(self) -> int:
        """Apply tiered exits to positions past their max hold period."""
        count = 0
        for symbol, pos in list(self._risk.get_open_positions().items()):
            days_held = self._days_held(pos)
            if days_held is None:
                continue
            strategy = pos.get("strategy", "momentum")
            max_hold = _MAX_HOLD_DAYS.get(
                strategy.lower(), self._settings.HOLD_MAX_DAYS
            )
            if days_held < max_hold:
                continue

            current = fetch_current_price(symbol)
            entry = float(pos.get("entry_price", 0) or 0)
            if current is None or entry <= 0:
                continue
            unrealized_pct = (current - entry) / entry

            # Zombie: held at least 2x the max hold -> exit regardless.
            if days_held >= 2 * max_hold:
                self._force_exit(symbol, ExitReason.TIME_EXIT_ZOMBIE)
                count += 1
                continue

            if unrealized_pct < 0.03:
                reason = (
                    ExitReason.TIME_EXIT_LOSS
                    if unrealized_pct < 0
                    else ExitReason.TIME_EXIT_FLAT
                )
                self._force_exit(symbol, reason)
                count += 1
            elif unrealized_pct <= 0.08:
                # Moderate profit: move stop to breakeven, keep holding.
                self._raise_stop(symbol, entry)
            else:
                # Strong runner: trail stop to lock in half the gains.
                trail_to = entry + (current - entry) * 0.5
                self._raise_stop(symbol, trail_to)
        return count

    # --------------------------------------------------- 3. position health

    def _check_position_health(self) -> int:
        """Close positions whose technical setup has broken down."""
        count = 0
        for symbol, pos in list(self._risk.get_open_positions().items()):
            df = fetch_ohlcv(symbol)
            if df is None or len(df) < self._settings.MIN_OHLCV_ROWS:
                continue
            strategy = pos.get("strategy", "momentum")

            # Re-score with the same engine.  A hard veto (None) counts as a
            # collapsed setup.
            signal = score_symbol(symbol, strategy, df)
            score = signal.signal_strength if signal is not None else 0.0
            if score >= _HEALTH_SCORE_FLOOR:
                continue

            entry = float(pos.get("entry_price", 0) or 0)
            stop = float(pos.get("stop_price", 0) or 0)
            current = float(df["Close"].iloc[-1])
            if entry <= 0 or stop >= entry:
                continue
            progress_to_stop = (entry - current) / (entry - stop)
            if progress_to_stop >= _HEALTH_STOP_PROGRESS:
                self._force_exit(symbol, ExitReason.SETUP_BROKEN)
                count += 1
        return count

    # --------------------------------------------------- 4. trailing stops

    def _check_trailing_stops(self) -> int:
        """Ratchet stops upward on profitable positions using 2x ATR."""
        if not self._settings.ENABLE_PARTIAL_TAKE_TRAIL:
            return 0
        count = 0
        for symbol, pos in list(self._risk.get_open_positions().items()):
            entry = float(pos.get("entry_price", 0) or 0)
            if entry <= 0:
                continue
            df = fetch_ohlcv(symbol, period="3mo")
            if df is None or len(df) < 20:
                continue
            current = float(df["Close"].iloc[-1])
            # Only trail once the trade is meaningfully in profit (> 5%).
            if (current - entry) / entry < 0.05:
                continue
            atr = self._atr(df)
            if atr <= 0:
                continue
            new_stop = current - 2.0 * atr
            if new_stop > float(pos.get("stop_price", 0) or 0):
                if self._raise_stop(symbol, new_stop):
                    count += 1
        return count

    # ------------------------------------------------------------- helpers

    def _finalise_exit(self, event: ExitEvent) -> None:
        """Journal an exit, drop it from the risk manager, record P&L.

        The broker attaches the exit commission to ``fill_details`` so the
        journal can compute a net P&L; the daily-loss accumulator is charged
        the same commission so the loss limit is measured on a net basis.
        """
        exit_commission = float(event.fill_details.get("commission", 0.0) or 0.0)
        self._journal.log_exit(event.symbol, event, exit_commission=exit_commission)
        self._risk.remove_position(event.symbol, event)
        self._risk.record_daily_pnl(event.pnl_gross - exit_commission)
        self._log.info(
            "exit.finalised",
            symbol=event.symbol,
            reason=event.exit_reason.value,
            pnl_gross=round(event.pnl_gross, 2),
            exit_commission=round(exit_commission, 4),
        )

    def _force_exit(self, symbol: str, reason: ExitReason) -> None:
        """Close *symbol* at market via the broker and finalise it."""
        # Both PaperBroker and IBKRBroker implement force_close; use it on
        # either so forced exits actually hit the market on the live path.
        event: Optional[ExitEvent] = self._broker.force_close(symbol, reason)
        if event is None:
            # Broker had no tracked position: synthesise from risk state.
            pos = self._risk.get_open_positions().get(symbol)
            price = fetch_current_price(symbol)
            entry = float(pos.get("entry_price", 0) or 0) if pos else 0.0
            qty = int(pos.get("quantity", 0) or 0) if pos else 0
            if price is None:
                price = entry
            event = ExitEvent(
                symbol=symbol,
                exit_price=round(price, 4),
                exit_reason=reason,
                exit_date=datetime.now(),
                pnl_gross=round((price - entry) * qty, 2),
            )
        self._finalise_exit(event)

    def _raise_stop(self, symbol: str, new_stop: float) -> bool:
        """Ratchet the broker stop and mirror it into the risk manager."""
        moved = self._broker.modify_stop(symbol, new_stop)
        if moved:
            self._risk.update_stop(symbol, new_stop)
        return moved

    def _days_held(self, pos: Dict[str, Any]) -> Optional[float]:
        """Return calendar days a position has been held, or ``None``."""
        ts = pos.get("entry_time")
        if not ts:
            return None
        try:
            entry_time = datetime.fromisoformat(str(ts))
        except (ValueError, TypeError):
            return None
        return (datetime.now() - entry_time).total_seconds() / 86_400.0

    @staticmethod
    def _atr(df: pd.DataFrame, period: int = 14) -> float:
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)
        tr = pd.concat(
            [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
            axis=1,
        ).max(axis=1)
        val = tr.rolling(window=period).mean().iloc[-1]
        return float(val) if not pd.isna(val) else 0.0
