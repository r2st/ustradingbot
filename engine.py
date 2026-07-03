"""
Main trading loop orchestrator for the US Trading Bot.

The :class:`TradingEngine` runs a continuous cycle of:
    1. Exit management (broker fills, time-based, health checks, trailing stops)
    2. Entry scanning (signal generation, risk checks, order placement)

It respects market hours (09:30-16:00 Eastern, weekdays only), handles
graceful shutdown via ``SIGINT``/``SIGTERM``, and logs every decision
through structlog for full auditability.

Usage::

    python engine.py

Or programmatically::

    from engine import TradingEngine
    import asyncio

    engine = TradingEngine()
    asyncio.run(engine.run())
"""

from __future__ import annotations

import asyncio
import signal
import sys
from datetime import datetime, time as dt_time, timedelta
from typing import List, Tuple
from zoneinfo import ZoneInfo

import structlog

import logging_config
from config.settings import Settings, get_settings
from config.universe import ALL_SYMBOLS, get_currency
from data.fetcher import fetch_current_price
from signals.screener import run_full_scan
from signals.signal_types import Signal, TradeOrder

log = structlog.get_logger(__name__)

# US Eastern timezone used for all market-hours logic.
ET = ZoneInfo("America/New_York")


class TradingEngine:
    """Core trading loop that orchestrates scanning, risk, and execution.

    The engine runs on a configurable interval
    (:attr:`Settings.SCAN_INTERVAL_MINUTES`), checks market hours,
    manages exits before entries, and routes each qualifying signal
    through the full entry pipeline.

    Attributes:
        settings: Application configuration singleton.
        risk_manager: Position sizing and risk gating (connected on init).
        trade_logger: CSV trade journal for entries and exits.
        rejected_logger: BTST (below-standard trade) rejection logger.
        running: Flag for graceful shutdown coordination.
    """

    def __init__(self) -> None:
        """Initialise the trading engine and all dependent subsystems."""
        self.settings: Settings = get_settings()
        self.running: bool = True

        # TODO: Uncomment once risk/manager.py is complete:
        # from risk.manager import RiskManager
        # self.risk_manager = RiskManager()
        self.risk_manager = None  # placeholder

        # TODO: Uncomment once journal/trade_logger.py is complete:
        # from journal.trade_logger import TradeLogger
        # self.trade_logger = TradeLogger()
        self.trade_logger = None  # placeholder

        # TODO: Uncomment once journal/btst_logger.py is complete:
        # from journal.btst_logger import BTSTLogger
        # self.rejected_logger = BTSTLogger()
        self.rejected_logger = None  # placeholder

        log.info(
            "engine.init",
            paper_trading=self.settings.IS_PAPER_TRADING,
            scan_interval_min=self.settings.SCAN_INTERVAL_MINUTES,
            max_positions=self.settings.MAX_OPEN_POSITIONS,
            capital=self.settings.TOTAL_CAPITAL,
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the main trading loop until shutdown is requested.

        Each iteration:
            1. Checks whether the market is currently open.
            2. If open, executes one full :meth:`run_cycle`.
            3. Sleeps for ``SCAN_INTERVAL_MINUTES`` before repeating.

        Registers ``SIGINT`` and ``SIGTERM`` handlers for graceful
        shutdown.
        """
        # Register OS signal handlers.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.shutdown)

        log.info("engine.started")

        while self.running:
            try:
                if self.is_market_open():
                    await self.run_cycle()
                else:
                    now_et = datetime.now(tz=ET)
                    log.info(
                        "engine.market_closed",
                        current_time_et=now_et.strftime("%Y-%m-%d %H:%M:%S %Z"),
                        weekday=now_et.strftime("%A"),
                    )
            except Exception:
                log.exception("engine.cycle_error")

            if not self.running:
                break

            sleep_seconds = self.settings.SCAN_INTERVAL_MINUTES * 60
            log.info(
                "engine.sleeping",
                sleep_minutes=self.settings.SCAN_INTERVAL_MINUTES,
            )

            try:
                await asyncio.sleep(sleep_seconds)
            except asyncio.CancelledError:
                break

        log.info("engine.stopped")

    # ------------------------------------------------------------------
    # Single cycle
    # ------------------------------------------------------------------

    async def run_cycle(self) -> None:
        """Execute one complete scan-and-trade cycle.

        The cycle is split into two phases:

        **Exit phase** (steps 1-4) — manage existing positions first so
        freed capital is available for new entries:
            1. Check broker for filled exit orders.
            2. Check time-based exit rules.
            3. Check position health (setup-broken conditions).
            4. Update trailing stops.

        **Entry phase** (steps 5-9) — scan for new signals and route
        them through the full entry pipeline:
            5. Order cutoff check (skip entries near market close).
            6. Run the screener on the full symbol universe.
            7. For each signal, run the entry pipeline.

        All steps log their actions via structlog for auditability.
        """
        cycle_start = datetime.now(tz=ET)
        log.info("engine.cycle_start", time_et=cycle_start.strftime("%H:%M:%S"))

        # ── Exit phase ────────────────────────────────────────────────
        await self._check_broker_exits()
        await self._check_time_based_exits()
        await self._check_position_health()
        await self._check_trailing_stops()

        # ── Entry phase ───────────────────────────────────────────────

        # Step 5: Order cutoff — skip new entries when too close to close.
        if self._is_past_order_cutoff():
            log.info(
                "engine.order_cutoff",
                cutoff_minutes=self.settings.ORDER_CUTOFF_MINUTES_BEFORE_CLOSE,
                message="Skipping new entries — too close to market close",
            )
            return

        # Step 6: Run the screener.
        signals: List[Signal] = run_full_scan(ALL_SYMBOLS, min_grade="B")

        # Step 7: Process each signal through the entry pipeline.
        trades_placed: int = 0
        trades_rejected: int = 0

        for sig in signals:
            placed = await self._process_signal(sig)
            if placed:
                trades_placed += 1
            else:
                trades_rejected += 1

        # Cycle summary.
        cycle_elapsed = (datetime.now(tz=ET) - cycle_start).total_seconds()
        log.info(
            "engine.cycle_complete",
            signals_found=len(signals),
            trades_placed=trades_placed,
            trades_rejected=trades_rejected,
            elapsed_seconds=round(cycle_elapsed, 2),
        )

    # ------------------------------------------------------------------
    # Exit management (all placeholders pending IBKR integration)
    # ------------------------------------------------------------------

    async def _check_broker_exits(self) -> None:
        """Poll the broker for filled exit orders and reconcile positions.

        TODO: Connect to IBKR via ib_insync.  Query open orders and
              execution reports.  For each filled exit:
              - Call risk_manager.remove_position(symbol)
              - Call trade_logger.log_exit(exit_event)
              - Update P&L tracking
        """
        log.debug("engine.check_broker_exits", status="placeholder")

    async def _check_time_based_exits(self) -> None:
        """Exit positions that have exceeded their maximum hold period.

        TODO: Iterate risk_manager.get_open_positions().  For each
              position where (now - entry_date).days > HOLD_MAX_DAYS:
              - If P&L < 0: submit market sell, reason=TIME_EXIT_LOSS
              - If P&L ~ 0: submit market sell, reason=TIME_EXIT_FLAT
              - If position is a zombie (no fills, no movement):
                reason=TIME_EXIT_ZOMBIE
        """
        log.debug("engine.check_time_based_exits", status="placeholder")

    async def _check_position_health(self) -> None:
        """Check whether any position's original setup has broken down.

        TODO: For each open position, re-run a subset of indicators
              to detect:
              - Price dropped below EMA-200 (bear regime)
              - Bearish volume surge (distribution day)
              - Ripster clouds crossed bearish
              If setup is broken, submit market sell,
              reason=SETUP_BROKEN
        """
        log.debug("engine.check_position_health", status="placeholder")

    async def _check_trailing_stops(self) -> None:
        """Update trailing stops for positions that have moved in our favour.

        TODO: For each open position with partial-take/trail enabled:
              - Recalculate stop based on current ATR
              - If new stop > existing stop, submit modify order to IBKR
              - Log the stop update
        """
        log.debug("engine.check_trailing_stops", status="placeholder")

    # ------------------------------------------------------------------
    # Entry pipeline
    # ------------------------------------------------------------------

    async def _process_signal(self, sig: Signal) -> bool:
        """Route a single signal through the full entry pipeline.

        Returns ``True`` if a trade was placed, ``False`` if rejected
        at any gate.

        The pipeline gates (in order):
            a) Risk manager pre-check (position limits, daily loss).
            b) Strategy capacity check.
            c) Pending order guard (avoid duplicate orders).
            d) AI evaluation (cost-gated Claude analysis).
            e) Order building (position sizing).
            f) Freshness check (price drift, signal age).
            g) Cash availability check.
            h) Bracket order placement.
            i) Logging and position registration.

        Args:
            sig: The qualifying signal from the screener.

        Returns:
            ``True`` if the trade was successfully placed, ``False``
            otherwise.
        """
        bound_log = log.bind(symbol=sig.symbol, strategy=sig.strategy)

        # (a) Risk manager pre-check.
        # TODO: Uncomment when risk_manager is available:
        # if not self.risk_manager.pre_check(sig):
        #     bound_log.info("engine.rejected", gate="pre_check")
        #     return False
        bound_log.debug("engine.gate_passed", gate="pre_check", status="placeholder")

        # (b) Strategy capacity check.
        # TODO: Uncomment when risk_manager is available:
        # if not self.risk_manager.check_strategy_cap(sig.strategy):
        #     bound_log.info("engine.rejected", gate="strategy_cap")
        #     return False
        bound_log.debug("engine.gate_passed", gate="strategy_cap", status="placeholder")

        # (c) Pending order guard.
        # TODO: Query IBKR for open/pending orders for this symbol.
        #       If an order already exists, skip to avoid doubling up.
        bound_log.debug("engine.gate_passed", gate="pending_order_guard", status="placeholder")

        # (d) AI evaluation.
        # TODO: Call the AI veto layer (ai/evaluator.py) for a Claude-based
        #       analysis of the signal.  For now, auto-approve with a log.
        ai_decision = "APPROVE"
        ai_reasoning = "AI evaluation layer pending — auto-approved"
        ai_cost = 0.0
        bound_log.info(
            "engine.ai_evaluation",
            decision=ai_decision,
            message="AI layer is pending — auto-approving all signals",
        )

        # (e) Build the order (position sizing).
        # TODO: Uncomment when risk_manager is available:
        # order: TradeOrder = self.risk_manager.build_order(
        #     sig, ai_decision, ai_reasoning, ai_cost
        # )
        # if order.quantity <= 0:
        #     bound_log.info("engine.rejected", gate="build_order", reason="zero quantity")
        #     return False
        order = TradeOrder(
            signal=sig,
            quantity=0,
            ai_decision=ai_decision,
            ai_reasoning=ai_reasoning,
            ai_cost_usd=ai_cost,
        )
        bound_log.debug("engine.gate_passed", gate="build_order", status="placeholder")

        # (f) Freshness check.
        fresh, reason = self.freshness_check(sig)
        if not fresh:
            bound_log.info("engine.rejected", gate="freshness_check", reason=reason)
            # TODO: Log rejection to btst_logger when available:
            # self.rejected_logger.log_rejection(sig, reason)
            return False

        # (g) Cash availability check.
        currency = get_currency(sig.symbol)
        # TODO: Uncomment when risk_manager is available:
        # available_cash = self.risk_manager.get_available_cash(currency)
        # required_cash = sig.entry_price * order.quantity
        # if required_cash > available_cash:
        #     bound_log.info(
        #         "engine.rejected",
        #         gate="cash_check",
        #         required=required_cash,
        #         available=available_cash,
        #         currency=currency,
        #     )
        #     return False
        bound_log.debug(
            "engine.gate_passed",
            gate="cash_check",
            currency=currency,
            status="placeholder",
        )

        # (h) Place bracket order.
        # TODO: Submit a bracket order to IBKR via ib_insync:
        #       - Parent: LMT BUY at sig.entry_price, quantity=order.quantity
        #       - Take-profit: LMT SELL at sig.target_price
        #       - Stop-loss: STP SELL at sig.stop_price
        #       Capture the order ID for tracking.
        bound_log.info(
            "engine.order_placement",
            status="placeholder",
            entry_price=sig.entry_price,
            stop_price=sig.stop_price,
            target_price=sig.target_price,
            quantity=order.quantity,
            message="IBKR bracket order placement pending",
        )

        # (i) Log and register.
        # TODO: Uncomment when trade_logger and risk_manager are available:
        # self.trade_logger.log_entry(order)
        # self.risk_manager.register_position(order)
        bound_log.info(
            "engine.trade_logged",
            status="placeholder",
            message="Trade logging and position registration pending",
        )

        return True

    # ------------------------------------------------------------------
    # Market hours
    # ------------------------------------------------------------------

    def is_market_open(self) -> bool:
        """Check whether the US equity market is currently open.

        The market is considered open on weekdays between 09:30 and
        16:00 Eastern Time.  This does not account for US market
        holidays; a holiday calendar can be integrated later.

        Returns:
            ``True`` if the current time falls within regular trading
            hours on a weekday.
        """
        now = datetime.now(tz=ET)

        # Weekdays only (Monday=0, Friday=4).
        if now.weekday() > 4:
            return False

        market_open = dt_time(
            self.settings.MARKET_OPEN_HOUR,
            self.settings.MARKET_OPEN_MINUTE,
        )
        market_close = dt_time(
            self.settings.MARKET_CLOSE_HOUR,
            self.settings.MARKET_CLOSE_MINUTE,
        )

        return market_open <= now.time() < market_close

    def _is_past_order_cutoff(self) -> bool:
        """Check if the current time is within the order cutoff window.

        New entries are not permitted in the final
        ``ORDER_CUTOFF_MINUTES_BEFORE_CLOSE`` minutes before market
        close to avoid partial fills and end-of-day volatility.

        Returns:
            ``True`` if we are past the cutoff and should skip entries.
        """
        now = datetime.now(tz=ET)
        market_close_dt = now.replace(
            hour=self.settings.MARKET_CLOSE_HOUR,
            minute=self.settings.MARKET_CLOSE_MINUTE,
            second=0,
            microsecond=0,
        )
        cutoff_dt = market_close_dt - timedelta(
            minutes=self.settings.ORDER_CUTOFF_MINUTES_BEFORE_CLOSE,
        )
        return now >= cutoff_dt

    # ------------------------------------------------------------------
    # Freshness check
    # ------------------------------------------------------------------

    def freshness_check(self, sig: Signal) -> Tuple[bool, str]:
        """Validate that a signal is still fresh enough to act on.

        Two checks are performed:

        1. **Age check** — the signal must not be older than
           ``SIGNAL_MAX_AGE_MINUTES``.
        2. **Price drift check** — the current market price must not
           have drifted more than ``SIGNAL_FRESHNESS_TOLERANCE_PCT``
           from the signal's proposed entry price.

        Args:
            sig: The signal to validate.

        Returns:
            A tuple ``(passed, reason)`` where *passed* is ``True``
            if both checks pass, and *reason* is ``"passed"`` or a
            descriptive rejection string.
        """
        now = datetime.now()

        # 1. Age check.
        age = now - sig.timestamp
        max_age = timedelta(minutes=self.settings.SIGNAL_MAX_AGE_MINUTES)
        if age > max_age:
            age_minutes = age.total_seconds() / 60.0
            reason = (
                f"signal_too_old: age={age_minutes:.1f}min "
                f"> max={self.settings.SIGNAL_MAX_AGE_MINUTES}min"
            )
            log.info(
                "engine.freshness_failed",
                symbol=sig.symbol,
                check="age",
                reason=reason,
            )
            return False, reason

        # 2. Price drift check.
        current_price = fetch_current_price(sig.symbol)
        if current_price is None:
            reason = "price_unavailable: could not fetch current price"
            log.warning(
                "engine.freshness_failed",
                symbol=sig.symbol,
                check="price_drift",
                reason=reason,
            )
            return False, reason

        if sig.entry_price <= 0:
            reason = "invalid_entry_price: entry_price <= 0"
            return False, reason

        drift_pct = abs(current_price - sig.entry_price) / sig.entry_price
        tolerance = self.settings.SIGNAL_FRESHNESS_TOLERANCE_PCT

        if drift_pct > tolerance:
            reason = (
                f"price_drifted: drift={drift_pct:.4f} "
                f"({drift_pct * 100:.2f}%) > tolerance={tolerance * 100:.1f}%"
            )
            log.info(
                "engine.freshness_failed",
                symbol=sig.symbol,
                check="price_drift",
                current_price=current_price,
                entry_price=sig.entry_price,
                drift_pct=round(drift_pct, 4),
                tolerance=tolerance,
            )
            return False, reason

        return True, "passed"

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Request a graceful shutdown of the trading loop.

        Sets the :attr:`running` flag to ``False``, which causes the
        main loop in :meth:`run` to exit after the current sleep or
        cycle completes.
        """
        log.info("engine.shutdown_requested")
        self.running = False


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

if __name__ == "__main__":
    logging_config.setup_logging(log_level=get_settings().LOG_LEVEL)

    engine = TradingEngine()

    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        log.info("engine.keyboard_interrupt")
        engine.shutdown()
