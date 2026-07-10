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

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

import structlog

from config.settings import EASTERN, Settings
from data.fetcher import fetch_current_price, fetch_ohlcv
from execution.broker import Broker
from execution.stops import compute_dynamic_stop, resolve_stop_config
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
    #: Every exit/partial event finalised this sweep, for downstream alerting.
    events: list = field(default_factory=list)


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
        # Exit/partial events finalised during the current manage_exits sweep.
        self._events: list = []

    # ------------------------------------------------------------- entry pt

    def manage_exits(self) -> ExitSummary:
        """Run all four exit checks in order and return a summary."""
        self._reconcile_stores()
        self._events: list = []
        summary = ExitSummary()
        summary.broker_exits = self._check_broker_exits()
        summary.time_exits = self._check_time_based_exits()
        summary.health_exits = self._check_position_health()
        summary.trails_updated = self._check_dynamic_stops()
        summary.events = list(self._events)
        return summary

    def _reconcile_stores(self) -> None:
        """Log warnings when the risk manager and broker position sets diverge.

        The risk manager (``open_positions.json``) and the broker
        (``paper_broker.json`` or IBKR ``_brackets``) track positions
        independently.  If they diverge — one has a symbol the other doesn't —
        it means a past exit or entry partially failed.  This check runs at the
        top of every exit sweep so the operator is alerted quickly.
        """
        risk_symbols = set(self._risk.get_open_positions().keys())
        broker_symbols = set(self._broker.get_positions().keys())
        only_risk = risk_symbols - broker_symbols
        only_broker = broker_symbols - risk_symbols
        if only_risk:
            self._log.warning(
                "exit.reconcile_divergence",
                only_in_risk_manager=sorted(only_risk),
                detail="These positions are tracked by the risk manager but "
                       "not by the broker — they may lack stop-loss protection.",
            )
        if only_broker:
            self._log.warning(
                "exit.reconcile_divergence",
                only_in_broker=sorted(only_broker),
                detail="These positions are tracked by the broker but not by "
                       "the risk manager — they won't count toward limits.",
            )

    # --------------------------------------------------- 1. broker exits

    def _check_broker_exits(self) -> int:
        """Reconcile bracket legs that filled at the broker.

        A ``PARTIAL_TAKE`` event trims the position (records the realised slice,
        shrinks the open lot) without closing it; every other event finalises
        and closes the position.
        """
        count = 0
        for event in self._broker.poll_exits():
            if event.exit_reason == ExitReason.PARTIAL_TAKE:
                self._finalise_partial(event)
            else:
                self._finalise_exit(event)
            count += 1
        return count

    def _finalise_partial(self, event: ExitEvent) -> None:
        """Record a partial profit-take: journal the slice, shrink the lot."""
        exit_commission = float(event.fill_details.get("commission", 0.0) or 0.0)
        take_qty = int(event.fill_details.get("quantity", 0) or 0)
        self._journal.log_partial_exit(
            event.symbol, event, exit_commission=exit_commission
        )
        self._risk.reduce_position(event.symbol, take_qty)
        self._risk.record_daily_pnl(event.pnl_gross - exit_commission)
        self._events.append(event)
        self._log.info(
            "exit.partial_taken",
            symbol=event.symbol,
            quantity=take_qty,
            pnl_gross=round(event.pnl_gross, 2),
        )

    # --------------------------------------------------- 2. time-based exits

    @staticmethod
    def _is_short(pos: Dict[str, Any]) -> bool:
        """Whether *pos* is a short position (``direction == "short"``)."""
        return str(pos.get("direction", "long")).lower() == "short"

    @staticmethod
    def _is_manual(pos: Dict[str, Any]) -> bool:
        """Whether *pos* was entered by hand from the dashboard.

        Manual positions carry their own operator-defined exit ladder, so the
        automated time / health / trailing sweeps leave them alone — the
        broker-exit reconciliation (which handles their level fills) is the
        only automated path that touches them.  A future dynamic-stop engine
        can opt manual positions back in by re-pricing their stop levels.
        """
        return bool(pos.get("manual"))

    def _check_time_based_exits(self) -> int:
        """Apply tiered exits to positions past their max hold period."""
        count = 0
        for symbol, pos in list(self._risk.get_open_positions().items()):
            if self._is_manual(pos):
                continue
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
                if current is None:
                    self._log.warning(
                        "exit.time_check_skipped_no_price", symbol=symbol
                    )
                continue
            # Direction-aware unrealised move: a short profits as price falls.
            is_short = self._is_short(pos)
            if is_short:
                unrealized_pct = (entry - current) / entry
            else:
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
                # (modify_stop ratchets protectively per side: up for longs,
                # down for a short's buy-stop.)
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
            if self._is_manual(pos):
                continue
            # The health re-score uses the LONG scoring engine (a high score
            # means a healthy long setup), which is meaningless for a short —
            # shorts are protected by their ATR buy-stop and the time exits.
            if self._is_short(pos):
                continue
            df = fetch_ohlcv(symbol)
            if df is None or len(df) < self._settings.MIN_OHLCV_ROWS:
                if df is None:
                    self._log.warning(
                        "exit.health_check_skipped_no_data", symbol=symbol
                    )
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

    # --------------------------------------------------- 4. dynamic stops

    def _check_dynamic_stops(self) -> int:
        """Ratchet stops upward using the dynamic-stop engine.

        Evaluates the trailing, breakeven, and time-based-tightening mechanisms
        (all volatility-adjusted via ATR) for every open position, resolving
        per-strategy configuration overrides.  Each mechanism only ever raises
        the stop, and the highest (most protective) candidate wins.
        """
        if not (self._settings.ENABLE_DYNAMIC_STOPS
                and self._settings.ENABLE_PARTIAL_TAKE_TRAIL):
            return 0
        count = 0
        for symbol, pos in list(self._risk.get_open_positions().items()):
            if self._is_manual(pos):
                continue
            # The dynamic-stop engine only ratchets stops UP (long
            # protection); shorts keep their ATR buy-stop plus the
            # direction-aware time-exit trailing above.
            if self._is_short(pos):
                continue
            df = fetch_ohlcv(symbol, period="3mo")
            if df is None or len(df) < 20:
                if df is None:
                    self._log.warning(
                        "exit.dynamic_stop_skipped_no_data", symbol=symbol
                    )
                continue
            config = resolve_stop_config(
                self._settings, str(pos.get("strategy", "momentum"))
            )
            decision = compute_dynamic_stop(pos, df, config)
            if decision is None:
                continue
            if self._raise_stop(symbol, decision.new_stop):
                count += 1
                self._log.info(
                    "exit.stop_raised",
                    symbol=symbol,
                    new_stop=decision.new_stop,
                    mechanism=decision.reason,
                )
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
        self._events.append(event)
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
            if pos is not None and self._is_short(pos):
                pnl = (entry - price) * qty
            else:
                pnl = (price - entry) * qty
            event = ExitEvent(
                symbol=symbol,
                exit_price=round(price, 4),
                exit_reason=reason,
                exit_date=datetime.now(tz=EASTERN),
                pnl_gross=round(pnl, 2),
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
        # Persisted entry_time may be tz-naive (older records / paper broker
        # fills); assume Eastern so subtracting from the tz-aware ``now`` never
        # raises "can't subtract offset-naive and offset-aware datetimes".
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=EASTERN)
        return (datetime.now(tz=EASTERN) - entry_time).total_seconds() / 86_400.0
