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
from agent.alerts import AlertManager
from ai.analyst import AIAnalyst
from config.settings import Settings, get_settings
from config.watchlist import scan_symbols_for
from data.fetcher import fetch_current_price
from execution.broker import make_broker
from execution.exit_manager import ExitManager
from journal.btst_logger import RejectedSignalLogger
from journal.trade_logger import TradeLogger
from risk.manager import RiskManager
from signals.screener import run_full_scan, run_prescreen
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

        data_dir = str(self.settings.DATA_DIR)

        # Risk gatekeeper (position sizing, caps, cooldowns, daily P&L).
        self.risk_manager = RiskManager(self.settings)

        # Structured activity feed for the dashboard (monitoring F4) — every
        # write is best-effort and can never break the trading loop.
        from journal.activity_log import ActivityLogger

        self.activity = ActivityLogger(data_dir)
        self._cycle_id: str = ""

        # Trade rationale capture (monitoring F9): why each trade was taken,
        # persisted at entry time.
        from journal.rationale import RationaleStore

        self.rationale_store = RationaleStore(data_dir)
        # Rationale context for resting orders, keyed by broker order id, so
        # a scale-in/MOC fill reconciled cycles later still gets its record.
        self._pending_rationale: dict[str, dict] = {}

        # Journals.  Rejections are mirrored into the activity feed via the
        # callback so the two logs stay in lockstep gate-for-gate.
        self.trade_logger = TradeLogger(data_dir, trading_mode=self.settings.TRADING_MODE)
        self.rejected_logger = RejectedSignalLogger(
            data_dir, on_rejection=self._on_rejection_activity
        )

        # AI veto layer (OpenRouter).
        self.ai_analyst = AIAnalyst(self.settings)

        # Memory & learning layer (F1 + F2).  The LearningStore backs both the
        # reflection writer (fills learnings.jsonl after each close) and the
        # learnings guard (consults it before entries); the similar-setup guard
        # reads the trade journal directly.  All three are fail-open.
        from ai.reflection import ReflectionEngine
        from analytics.learnings_guard import LearningsGuard
        from journal.learnings import LearningStore

        self.learning_store = LearningStore(data_dir)
        self.reflection_engine = ReflectionEngine(self.settings, self.learning_store)
        self.learnings_guard = LearningsGuard(self.settings, self.learning_store)

        # Broker (paper by default; ibkr when configured).
        self.broker = make_broker(self.settings)
        if not self.broker.connect():
            # Do not crash on a failed initial connect: the run loop will retry
            # with exponential backoff before each cycle.  Log loudly so the
            # failure is visible.
            log.error(
                "engine.broker_connect_failed",
                broker=self.settings.BROKER,
                message="initial broker connect failed; will retry with backoff",
            )

        # Exit management coordinator.
        self.exit_manager = ExitManager(
            self.settings, self.broker, self.risk_manager, self.trade_logger
        )

        # Unified alerts (Telegram + email + threshold monitors).
        self.notifier = AlertManager(self.settings)

        # News-sentiment entry filter (feature 4) — fail-open when disabled.
        from data.news_sentiment import NewsSentimentFilter

        self.news_filter = NewsSentimentFilter(self.settings)

        # Earnings block/flag entry filter (Feature 1a) — off by default, PEAD
        # exempt.  Fail-open: a broken earnings calendar never halts the scan.
        from signals.earnings_filter import EarningsEntryFilter

        self.earnings_filter = EarningsEntryFilter(self.settings)

        # Ratings entry filter (Feature 5) — off by default, fail-open.
        from signals.ratings_filter import RatingsFilter

        self.ratings_filter = RatingsFilter(self.settings)

        # Overnight-gap entry filter (Feature 4) — off by default, fail-open.
        from signals.gap_filter import GapEntryFilter

        self.gap_filter = GapEntryFilter(self.settings)

        # Market-regime + auto-tune state (features 12, 13); refreshed each
        # cycle.  Start neutral so a data outage never blocks entries.
        from analytics.regime import RegimeResult
        from automation.autotune import TuneResult

        self._regime: RegimeResult = RegimeResult()
        self._autotune: TuneResult = TuneResult(
            enabled=False, applied=False, trades_considered=0, win_rate=0.0,
            delta=0.0, thresholds={}, reason="not yet run",
        )

        # In-process scheduler for P&L reports + nightly backtests (features 10,
        # 11).  ``None`` when SCHEDULER_ENABLED is off.
        from automation.scheduler import build_scheduler

        self._scheduler = build_scheduler(self.settings)

        # Resting entry orders (scale-in tranches / limit / MOC) awaiting a
        # fill, keyed by the broker order id -> the built TradeOrder.  Filled
        # tranches are journaled and registered when poll_pending_entries
        # reports them; expired orders are dropped.
        self._pending_orders: dict[str, TradeOrder] = {}

        # Dashboard status heartbeat: last completed cycle and next scan time
        # (ISO strings in market time), surfaced through the engine_status file.
        self._last_cycle_at: str | None = None
        self._next_scan_at: str | None = None

        # Set by shutdown() to interrupt the between-cycle sleep immediately, so
        # SIGTERM (e.g. a dashboard-triggered `systemctl stop/restart`) is
        # honoured within a second instead of waiting out a 60-minute sleep.
        self._stop_event: asyncio.Event | None = None

        # ── Tiered scanning state ────────────────────────────────────────
        # Tier 2 (Scan Pool) runs once per calendar day; Tier 3 (Universe)
        # once per ISO week.  Both are keyed off a date/week string so a
        # restart mid-day/week doesn't re-run them.  ``_tier1_symbol_set`` is
        # the current Active-Trading scan set, used to skip symbols already
        # covered every cycle when scanning the wider tiers.
        self._last_tier2_date: str | None = None
        self._last_tier3_week: str | None = None
        self._tier1_symbol_set: set[str] = set()
        # (legacy sector-rotation state — retained for back-compat helpers)
        self._sector_rotation_idx: int = 0
        self._last_tier2_time: datetime | None = None
        self._last_tier3_date: str | None = None

        log.info(
            "engine.init",
            broker=self.settings.BROKER,
            paper_trading=self.settings.IS_PAPER_TRADING,
            ai_model=self.settings.OPENROUTER_MODEL,
            ai_veto_enabled=self.settings.AI_VETO_ENABLED,
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
        # Register OS signal handlers.  The stop event must be created inside
        # the running loop so shutdown() can wake the sleep from a signal.
        loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.shutdown)

        log.info("engine.started")
        self._emit_heartbeat("starting")

        # Kick off the background scheduler (P&L reports, nightly backtests).
        if self._scheduler is not None:
            self._scheduler.start()

        while self.running:
            # A dashboard mode switch drops a restart sentinel; when we see it
            # we re-exec so the new BROKER/IBKR_PORT from .env takes effect.
            if self._restart_requested():
                self._restart_process()

            try:
                if self.is_market_open():
                    # Guard every cycle behind a live broker connection; a
                    # dropped session is transparently re-established (with
                    # exponential backoff) before we scan or manage exits.
                    if await self._ensure_broker_connected():
                        self._emit_heartbeat("scanning")
                        await self.run_cycle()
                        self._last_cycle_at = datetime.now(tz=ET).isoformat()
                    else:
                        log.error(
                            "engine.cycle_skipped_no_broker",
                            message="broker unavailable after retries; skipping cycle",
                        )
                else:
                    now_et = datetime.now(tz=ET)
                    log.info(
                        "engine.market_closed",
                        current_time_et=now_et.strftime("%Y-%m-%d %H:%M:%S %Z"),
                        weekday=now_et.strftime("%A"),
                    )
            except Exception as exc:
                log.exception("engine.cycle_error")
                self.activity.log(
                    "error", cycle_id=self._cycle_id, message=str(exc)[:300]
                )

            if not self.running:
                break

            sleep_seconds = self.settings.SCAN_INTERVAL_MINUTES * 60
            self._next_scan_at = (
                datetime.now(tz=ET) + timedelta(seconds=sleep_seconds)
            ).isoformat()
            log.info(
                "engine.sleeping",
                sleep_minutes=self.settings.SCAN_INTERVAL_MINUTES,
                realtime_exits=self._realtime_exits_enabled(),
            )
            self._emit_heartbeat("market_closed" if not self.is_market_open()
                                 else "waiting")

            try:
                await self._sleep_between_cycles(sleep_seconds)
            except asyncio.CancelledError:
                break

        log.info("engine.stopped")
        self._emit_heartbeat("stopped")

    def _emit_heartbeat(self, phase: str) -> None:
        """Write a status heartbeat the dashboard reads (best-effort).

        Reports the current phase, market state, open-position count, and the
        last-cycle / next-scan timestamps so the dashboard's engine control
        panel can show live activity without scraping logs.
        """
        import os

        from dashboard.engine_control import write_heartbeat

        try:
            # Adopt any positions the dashboard process wrote to the shared
            # state file so the reported count matches what the dashboard shows.
            self.risk_manager.sync_positions_from_disk()
            open_positions = len(self.risk_manager.get_open_positions())
        except Exception:  # noqa: BLE001 -- never let telemetry break the loop
            open_positions = None

        write_heartbeat(
            self.settings.DATA_DIR,
            phase=phase,
            pid=os.getpid(),
            market_open=self.is_market_open(),
            scan_interval_min=self.settings.SCAN_INTERVAL_MINUTES,
            open_positions=open_positions,
            last_cycle_at=self._last_cycle_at,
            next_scan_at=self._next_scan_at,
        )

    def _realtime_exits_enabled(self) -> bool:
        """Return whether fast exit polling should run between scan cycles.

        Enabled only when a realtime-capable data provider is active (e.g.
        Alpaca) and ``ENABLE_REALTIME_EXITS`` is set — the default daily-bar
        yfinance provider gains nothing from sub-minute polling.
        """
        return (
            self.settings.ENABLE_REALTIME_EXITS
            and self.settings.is_realtime_provider()
        )

    async def _sleep_between_cycles(self, sleep_seconds: float) -> None:
        """Wait until the next scan, polling exits often on a realtime feed.

        With a daily-bar provider this is a single ``asyncio.sleep``.  With a
        realtime provider it instead wakes every ``REALTIME_EXIT_POLL_SECONDS``
        to run exit management only (no scanning), so stops and targets are
        acted on with low latency while entries stay on the slower
        ``SCAN_INTERVAL_MINUTES`` cadence.
        """
        if not self._realtime_exits_enabled():
            await self._interruptible_sleep(sleep_seconds)
            return

        poll = max(1.0, float(self.settings.REALTIME_EXIT_POLL_SECONDS))
        elapsed = 0.0
        while elapsed < sleep_seconds and self.running:
            await self._interruptible_sleep(min(poll, sleep_seconds - elapsed))
            elapsed += poll
            if not self.running or not self.is_market_open():
                continue
            if self.broker.is_connected():
                try:
                    self.exit_manager.manage_exits()
                except Exception:
                    log.exception("engine.realtime_exit_error")

    async def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep for *seconds*, returning early if shutdown is requested.

        Waits on the stop event with a timeout: a normal wait times out and the
        sleep completes, but ``shutdown()`` sets the event to return at once so
        the loop exits promptly on SIGINT/SIGTERM.
        """
        stop_event = getattr(self, "_stop_event", None)
        if stop_event is None:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass  # the full interval elapsed — a normal, uninterrupted sleep

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
        self._cycle_id = cycle_start.strftime("%Y%m%d-%H%M%S")
        log.info("engine.cycle_start", time_et=cycle_start.strftime("%H:%M:%S"))
        self.activity.log("cycle_start", cycle_id=self._cycle_id)

        # Day-boundary detection: reset the daily P&L accumulator when we cross
        # into a new US trading day so the daily loss limit measures today only.
        if self.risk_manager.maybe_reset_daily_pnl():
            log.info("engine.daily_pnl_reset", date=cycle_start.strftime("%Y-%m-%d"))

        # Adopt position changes made by the dashboard process (manual trades
        # share open_positions.json) before the exit/entry phases, so manual
        # positions are exit-managed and duplicate entries are blocked.
        self.risk_manager.sync_positions_from_disk()

        # Refresh market regime + adaptive thresholds for this cycle (both are
        # best-effort and never raise into the loop).
        self._refresh_regime_and_autotune()

        # ── Pending-entry reconciliation ──────────────────────────────
        # Fill/expire resting entry orders (scale-in tranches, limit, MOC)
        # placed on prior cycles before doing anything else.
        pending_filled = await self._reconcile_pending_entries()
        if pending_filled:
            log.info("engine.pending_entries_filled", count=pending_filled)

        # ── Exit phase ────────────────────────────────────────────────
        # A single coordinator runs broker reconciliation, time-based exits,
        # position-health exits, and trailing-stop updates.
        exit_summary = self.exit_manager.manage_exits()
        total_exits = (
            exit_summary.broker_exits
            + exit_summary.time_exits
            + exit_summary.health_exits
        )
        if total_exits or exit_summary.trails_updated:
            log.info(
                "engine.exit_phase",
                broker_exits=exit_summary.broker_exits,
                time_exits=exit_summary.time_exits,
                health_exits=exit_summary.health_exits,
                trails_updated=exit_summary.trails_updated,
            )
            if exit_summary.trails_updated:
                self.activity.log(
                    "trail_updated",
                    cycle_id=self._cycle_id,
                    count=exit_summary.trails_updated,
                )

        # Alert on every finalised exit / partial-take, then evaluate the
        # drawdown and daily-loss threshold monitors.
        for event in exit_summary.events:
            await self.notifier.notify_exit(event)
            self.activity.log(
                "exit",
                cycle_id=self._cycle_id,
                symbol=event.symbol,
                reason=event.exit_reason.value,
                exit_price=round(event.exit_price, 4),
                pnl_gross=round(event.pnl_gross, 2),
            )
            self._push_notify(
                f"Exit: {event.symbol}",
                f"Closed {event.symbol} @ {event.exit_price:.2f} "
                f"({event.exit_reason.value}), P&L {event.pnl_gross:+.2f}",
            )
        await self._check_risk_alerts()

        # Trade Reflection (F1): after exits are finalised, write one plain-
        # English lesson per genuinely-closed trade to learnings.jsonl so future
        # entries can learn from them.  Best-effort and fully fail-open — a
        # reflection error never disturbs the trading loop.
        if self.settings.LEARNINGS_ENABLED and exit_summary.events:
            await self._reflect_on_exits(exit_summary.events)

        # Proximity alerts (F3/F6): warn once per symbol per day when price
        # is within POSITION_PROXIMITY_ALERT_PCT of a stop or target.
        # Engine-side so alerts fire even with no browser open.
        await self._check_proximity_alerts()

        # ── Entry phase ───────────────────────────────────────────────

        # Step 5: Order cutoff — skip new entries when too close to close,
        # UNLESS market-on-close entries are enabled, in which case signals
        # inside the cutoff window are routed to MOC orders instead of skipped.
        if self._is_past_order_cutoff() and not self.settings.ENABLE_MOC_ENTRIES:
            log.info(
                "engine.order_cutoff",
                cutoff_minutes=self.settings.ORDER_CUTOFF_MINUTES_BEFORE_CLOSE,
                message="Skipping new entries — too close to market close",
            )
            return

        # Step 6: Run the screener over the user-managed watchlist (falls back
        # to the built-in universe when the watchlist feature is disabled/empty),
        # restricted by the dashboard trade selection (feature 1): the operator
        # can pin the engine to chosen symbols / strategies / a minimum grade
        # based on backtest results.  Re-read each cycle so a dashboard save
        # takes effect on the next scan without a restart.
        from config.trade_selection import load_trade_selection

        selection = load_trade_selection(self.settings.DATA_DIR)
        # Remembered for the entry pipeline's hard selection gate: every
        # signal — whatever scan produced it — is re-checked against the
        # operator's selection before an order can be built.
        self._selection = selection
        # Tier 1 (Active Trading) = watchlist ∪ ETFs ∪ auto-promoted symbols.
        # Expire stale promotions first, then fold the survivors into the scan
        # set so a symbol promoted by a Tier 2/3 signal gets full every-cycle
        # evaluation until its promotion lapses.  Best-effort — a DB hiccup
        # never costs the base watchlist scan.
        base_symbols = scan_symbols_for(self.settings)
        try:
            from config.universe import expire_promotions, get_promoted_tier1_symbols

            expire_promotions()
            promoted = get_promoted_tier1_symbols()
            if promoted:
                base_symbols = sorted(set(base_symbols) | set(promoted))
                log.info("engine.tier1_promotions_active", count=len(promoted))
        except Exception:  # noqa: BLE001
            log.debug("engine.promotion_merge_failed", exc_info=True)
        scan_symbols = selection.filter_symbols(base_symbols)
        # Remember the active-trading set so the wider tiers can skip symbols
        # already scanned every cycle.
        self._tier1_symbol_set = set(scan_symbols)
        if selection.enabled:
            log.info(
                "engine.trade_selection_active",
                symbols=len(scan_symbols),
                strategies=selection.strategies or "all",
                min_grade=selection.min_grade,
            )
        signals: List[Signal] = run_full_scan(
            scan_symbols,
            min_grade=selection.effective_min_grade("B"),
            allowed_strategies=selection.allowed_strategies(),
            max_workers=self.settings.TIER1_WORKERS,
        )

        # Step 6a: Tiered scanning — Tier 2 (daily S&P 500 Scan Pool) + Tier 3
        # (weekly S&P 500 ∪ NASDAQ-100 Universe sweep).  A signal from either
        # tier auto-promotes its symbol into Tier 1 for full every-cycle
        # evaluation.  Runs only when the universe database is available.
        tiered_signals = self._run_tiered_scans(selection)
        if tiered_signals:
            signals.extend(tiered_signals)

        # Step 6b: Short-side scan (short_strategies module).  Short signals
        # join the same list and flow through the identical entry pipeline —
        # every gate below is direction-aware.  Best-effort: a failure in the
        # short module must never cost the long scan.
        try:
            short_signals = self._run_short_scan(scan_symbols, selection)
            signals.extend(short_signals)
        except Exception:  # noqa: BLE001
            log.exception("engine.short_scan_error")
            short_signals = []

        # Step 6c: Highly selective strategies scan.  Best-effort: a failure
        # in the selective module must never cost the long or short scan.
        selective_signals: List[Signal] = []
        try:
            selective_signals = self._run_selective_scan(scan_symbols, selection)
            signals.extend(selective_signals)
        except Exception:  # noqa: BLE001
            log.exception("engine.selective_scan_error")

        # Step 6d: Sector-rotation scan (Feature 3).  Ranks the 11 sector ETFs
        # by relative strength vs SPY and goes long the top-N leaders.  Off by
        # default; best-effort so a failure never costs the other scans.
        try:
            rotation_signals = self._run_sector_rotation_scan(selection)
            signals.extend(rotation_signals)
        except Exception:  # noqa: BLE001
            log.exception("engine.sector_rotation_scan_error")

        self.activity.log(
            "scan_complete",
            cycle_id=self._cycle_id,
            symbols_scanned=len(scan_symbols),
            signals_found=len(signals),
            short_signals=len(short_signals),
            selective_signals=len(selective_signals),
        )
        # Persist the scan's signal list for the watchlist monitor (F8).
        from journal.activity_log import write_last_scan

        write_last_scan(self.settings.DATA_DIR, self._cycle_id, signals)

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
        open_positions = len(self.risk_manager.get_open_positions())
        log.info(
            "engine.cycle_complete",
            signals_found=len(signals),
            trades_placed=trades_placed,
            trades_rejected=trades_rejected,
            open_positions=open_positions,
            ai_cost_usd=self.ai_analyst.total_cost_usd,
            elapsed_seconds=round(cycle_elapsed, 2),
        )
        self.activity.log(
            "cycle_complete",
            cycle_id=self._cycle_id,
            signals_found=len(signals),
            trades_placed=trades_placed,
            trades_rejected=trades_rejected,
            exits=total_exits,
            open_positions=open_positions,
            elapsed_seconds=round(cycle_elapsed, 2),
        )
        await self.notifier.notify_cycle(
            signals_found=len(signals),
            trades_placed=trades_placed,
            exits=total_exits,
            open_positions=open_positions,
        )

    def _run_short_scan(self, scan_symbols, selection) -> List[Signal]:
        """Run the short-strategy scan for this cycle (empty when disabled).

        The trade-selection strategy whitelist is honoured: when the operator
        pinned specific strategies, only whitelisted ``short_*`` ids run (a
        whitelist of long-only strategies disables shorts for the cycle).
        """
        from short_strategies import get_short_config, run_short_scan

        if not get_short_config().enabled:
            return []
        allowed = selection.allowed_strategies()
        if allowed is not None:
            allowed = [s for s in allowed if str(s).startswith("short_")]
            if not allowed:
                return []
        return run_short_scan(
            scan_symbols,
            min_grade=selection.effective_min_grade("B"),
            allowed_strategies=allowed,
            open_positions=self.risk_manager.get_open_positions(),
        )

    def _run_selective_scan(self, scan_symbols, selection) -> List[Signal]:
        """Run the highly selective strategy scan (empty when disabled).

        The trade-selection strategy whitelist is honoured: when the operator
        pinned specific strategies, only whitelisted ``hs_*`` ids run (a
        whitelist of non-selective strategies disables selective for the cycle).
        """
        from selective_strategies import get_selective_config, run_selective_scan

        if not get_selective_config().enabled:
            return []
        allowed = selection.allowed_strategies()
        if allowed is not None:
            allowed = [s for s in allowed if str(s).startswith("hs_")]
            if not allowed:
                return []
        return run_selective_scan(
            scan_symbols,
            min_grade=selection.effective_min_grade("B"),
            allowed_strategies=allowed,
        )

    def _run_sector_rotation_scan(self, selection) -> List[Signal]:
        """Run the sector-rotation scan (Feature 3); empty when disabled.

        Honours the trade-selection strategy whitelist: when the operator pinned
        specific strategies, ``sector_rotation`` must be among them for the scan
        to run.
        """
        if not getattr(self.settings, "SECTOR_ROTATION_ENABLED", False):
            return []
        allowed = selection.allowed_strategies()
        if allowed is not None and "sector_rotation" not in allowed:
            return []
        from signals.sector_rotation import run_sector_rotation_scan

        return run_sector_rotation_scan(
            self.settings,
            min_grade=selection.effective_min_grade("B"),
        )

    # ------------------------------------------------------------------
    # Tiered scanning (Full Stock Universe)
    # ------------------------------------------------------------------

    def _run_tiered_scans(self, selection) -> List[Signal]:
        """Run Tier 2 (daily Scan Pool) and Tier 3 (weekly Universe sweep).

        Only runs when the universe database is available and tiered scanning
        is enabled in settings.  Best-effort: failures never break the scan.
        Signals from either tier auto-promote their symbol into Tier 1.
        """
        if not self.settings.TIERED_SCANNING_ENABLED:
            return []
        try:
            from data_store.universe import db_exists

            if not db_exists(self.settings.DATA_DIR):
                return []
        except Exception:  # noqa: BLE001
            return []

        signals: List[Signal] = []

        # ── Tier 2: S&P 500 Scan Pool (once per day) ─────────────────────
        if self.settings.TIER2_ENABLED:
            try:
                signals.extend(self._run_tier2_scan(selection))
            except Exception:
                log.exception("engine.tier2_scan_error")

        # ── Tier 3: S&P 500 ∪ NASDAQ-100 Universe sweep (once per week) ──
        if self.settings.TIER3_ENABLED:
            try:
                signals.extend(self._run_tier3_scan(selection))
            except Exception:
                log.exception("engine.tier3_scan_error")

        return signals

    def _run_tier2_scan(self, selection) -> List[Signal]:
        """Tier 2: scan the top-N S&P 500 names by liquidity, once per day.

        The Scan Pool is the most liquid slice of the S&P 500 (ranked by
        volume × market cap).  Symbols already in the every-cycle Tier 1 set are
        skipped to avoid duplicate work.  Any signal promotes its symbol into
        Tier 1 so subsequent cycles evaluate it in full.
        """
        today = datetime.now(tz=ET).strftime("%Y-%m-%d")
        if self._last_tier2_date == today:
            return []

        from config.universe import get_scan_pool_symbols

        pool = get_scan_pool_symbols(limit=self.settings.TIER2_SCAN_POOL_SIZE)
        pool = [s for s in pool if s not in self._tier1_symbol_set]
        if not pool:
            return []
        # Respect the operator's trade selection just like the base scan.
        pool = selection.filter_symbols(pool)
        if not pool:
            self._last_tier2_date = today
            return []

        self._last_tier2_date = today
        log.info("engine.tier2_scan_pool", size=len(pool))
        signals = run_full_scan(
            pool,
            min_grade=selection.effective_min_grade("B"),
            allowed_strategies=selection.allowed_strategies(),
            max_workers=self.settings.TIER2_WORKERS,
        )
        self._promote_signals(signals, "tier2")
        if signals:
            log.info("engine.tier2_complete", scanned=len(pool), signals=len(signals))
        return signals

    def _run_tier3_scan(self, selection) -> List[Signal]:
        """Tier 3: pre-screen the full S&P 500 ∪ NASDAQ-100, once per week.

        A lightweight pre-screen finds unusual movers across the whole index
        universe; only those are full-scanned, keeping the weekly sweep cheap.
        Any signal promotes its symbol into Tier 1.
        """
        week = datetime.now(tz=ET).strftime("%G-W%V")  # ISO year-week
        if self._last_tier3_week == week:
            return []

        from config.universe import get_index_universe_symbols

        universe = get_index_universe_symbols()
        if not universe or len(universe) <= len(self.settings.CAPITAL_BY_CURRENCY):
            return []
        # Don't re-screen names already covered every cycle.
        universe = [s for s in universe if s not in self._tier1_symbol_set]

        log.info("engine.tier3_prescreen_start", total=len(universe))
        qualifying = run_prescreen(
            universe,
            price_change_pct=self.settings.TIER3_PRESCREEN_PRICE_CHANGE_PCT,
            volume_ratio=self.settings.TIER3_PRESCREEN_VOLUME_RATIO,
            max_workers=self.settings.TIER3_WORKERS,
        )
        self._last_tier3_week = week

        if not qualifying:
            log.info("engine.tier3_prescreen_none")
            return []
        qualifying = selection.filter_symbols(qualifying)
        if not qualifying:
            return []

        log.info("engine.tier3_full_scan", qualifying=len(qualifying))
        signals = run_full_scan(
            qualifying,
            min_grade=selection.effective_min_grade("B"),
            allowed_strategies=selection.allowed_strategies(),
            max_workers=self.settings.TIER2_WORKERS,
        )
        self._promote_signals(signals, "tier3")
        return signals

    def _promote_signals(self, signals: List[Signal], source_tier: str) -> None:
        """Promote every signalling symbol into Tier 1 (best-effort).

        Called after a Tier 2/3 scan.  Each promoted symbol joins the
        Active-Trading set for ``PROMOTION_TTL_HOURS`` so future cycles evaluate
        it in full.  Failures are swallowed — promotion must never break a scan.
        """
        if not signals:
            return
        from config.universe import promote_to_tier1

        promoted: set[str] = set()
        for sig in signals:
            symbol = getattr(sig, "symbol", "")
            if not symbol or symbol in promoted:
                continue
            promoted.add(symbol)
            reason = f"{getattr(sig, 'strategy', '')} {getattr(getattr(sig, 'grade', None), 'value', '')}".strip()
            try:
                promote_to_tier1(symbol, source_tier=source_tier, reason=reason)
            except Exception:  # noqa: BLE001
                log.debug("engine.promote_failed", symbol=symbol, exc_info=True)
        if promoted:
            log.info(
                "engine.tier_promotions",
                source_tier=source_tier,
                count=len(promoted),
                symbols=sorted(promoted),
            )

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

        # (a0) Trade-selection hard gate.  The scans already filter on the
        #      operator's selection, but this re-check guarantees no signal
        #      source can trade a symbol, strategy, or grade outside it —
        #      the definitive fix for "selected setup A, engine traded B".
        selection = getattr(self, "_selection", None)
        if selection is not None:
            allowed, reason = selection.allows_signal(sig)
            if not allowed:
                bound_log.info(
                    "engine.rejected", gate="trade_selection", reason=reason
                )
                self.rejected_logger.log_rejection(sig, "trade_selection", reason)
                return False

        # (a1) Earnings block/flag gate (Feature 1a).  Cheap, runs before the
        #      paid AI call so an earnings-proximate entry is rejected early.
        #      In flag mode it only annotates the signal and lets it through;
        #      PEAD is exempt.  Fail-open on any calendar error.
        earnings_check = self.earnings_filter.check(sig)
        if not earnings_check.allowed:
            bound_log.info(
                "engine.rejected", gate="earnings_filter", reason=earnings_check.reason
            )
            self.rejected_logger.log_rejection(
                sig, "earnings_filter", earnings_check.reason
            )
            return False
        if earnings_check.mode == "flag":
            bound_log.info("engine.earnings_flag", reason=earnings_check.reason)

        # (a) Risk manager pre-check (already-held, cooldown, daily loss,
        #     max positions, invalid stop/target, R:R minimum).
        ok, reason = self.risk_manager.pre_check(sig)
        if not ok:
            bound_log.info("engine.rejected", gate="pre_check", reason=reason)
            self.rejected_logger.log_rejection(sig, "pre_check", reason)
            return False

        # (b) Strategy capacity check.
        ok, reason = self.risk_manager.check_strategy_cap(sig.strategy)
        if not ok:
            bound_log.info("engine.rejected", gate="strategy_cap", reason=reason)
            self.rejected_logger.log_rejection(sig, "strategy_cap", reason)
            return False

        # (c) Pending order guard — never double up on a symbol the broker
        #     already holds.
        if sig.symbol in self.broker.get_positions():
            bound_log.info("engine.rejected", gate="pending_order_guard")
            self.rejected_logger.log_rejection(
                sig, "pending_order_guard", "broker already holds symbol"
            )
            return False

        # (d) AI evaluation (Tier-1 earnings filter + Tier-2 OpenRouter veto).
        decision = await self.ai_analyst.evaluate(sig)
        if not decision.approved:
            bound_log.info(
                "engine.rejected", gate="ai_veto", reason=decision.reasoning
            )
            self.rejected_logger.log_rejection(sig, "ai_veto", decision.reasoning)
            return False

        # (d1) Similar-Setup Guard (F2) — the bot checks its own record before
        #      entering.  Query the ledger for setups like this one (same
        #      strategy/grade, RSI & volume within tolerance); a poor historical
        #      win rate demotes the signal to grade-A-only or, if very poor over
        #      a solid sample, skips it.  Pure/local and fail-open.
        if self.settings.SIMILAR_SETUP_ENABLED:
            from analytics.setup_similarity import find_similar_setups
            from signals.signal_types import Grade

            similar = find_similar_setups(
                sig, self.trade_logger.csv_path, self.settings
            )
            if similar.blocks or (similar.demotes and sig.grade != Grade.A):
                bound_log.info(
                    "engine.rejected", gate="similar_setup", reason=similar.reason
                )
                self.rejected_logger.log_rejection(
                    sig, "similar_setup", similar.reason
                )
                return False

        # (d1.5) Learnings guard (F1) — apply the plain-English lessons the bot
        #        wrote after past closes.  ``avoid`` rejects; ``require_confirm``
        #        restricts to grade A; ``prefer`` only annotates.  Fail-open.
        if self.settings.LEARNINGS_ENABLED:
            from signals.signal_types import Grade

            verdict = self.learnings_guard.evaluate(sig)
            if verdict.rejects or (verdict.demotes and sig.grade != Grade.A):
                bound_log.info(
                    "engine.rejected", gate="learnings_guard", reason=verdict.reason
                )
                self.rejected_logger.log_rejection(
                    sig, "learnings_guard", verdict.reason
                )
                return False
            if verdict.prefer_notes:
                bound_log.info(
                    "engine.learnings_prefer", count=len(verdict.prefer_notes)
                )

        # (d2) News-sentiment veto (feature 4) — reject on strong negative news.
        news = await asyncio.to_thread(self.news_filter.check, sig.symbol)
        if not news.approved:
            bound_log.info("engine.rejected", gate="news_sentiment", reason=news.reason)
            self.rejected_logger.log_rejection(sig, "news_sentiment", news.reason)
            return False

        # (d4) Third-party ratings veto (Feature 5) — reject when the quant
        #      rating is below the operator's minimum.  Off by default and
        #      fail-open; grouped with news as an external-context veto.
        ratings = await asyncio.to_thread(self.ratings_filter.check, sig.symbol)
        if not ratings.approved:
            bound_log.info("engine.rejected", gate="ratings_filter", reason=ratings.reason)
            self.rejected_logger.log_rejection(sig, "ratings_filter", ratings.reason)
            return False

        # (d3) Market-regime + auto-tune floor (features 12, 13).  In a regime
        #      that disfavours this strategy family, or when a cold streak has
        #      raised the adaptive bar, only the strongest setups get through.
        ok, reason = self._regime_autotune_gate(sig)
        if not ok:
            bound_log.info("engine.rejected", gate="regime_autotune", reason=reason)
            self.rejected_logger.log_rejection(sig, "regime_autotune", reason)
            return False

        # (e) Build the sized order.
        order = self.risk_manager.build_order(
            sig, decision.decision, decision.reasoning, decision.cost_usd
        )
        if order is None or order.quantity <= 0:
            bound_log.info("engine.rejected", gate="build_order", reason="zero_qty")
            self.rejected_logger.log_rejection(
                sig, "build_order", "position size rounded to zero shares"
            )
            return False

        # (f2) Overnight-gap filter (Feature 4).  A "has the world moved since
        #      the setup?" check, like freshness: skip a morning entry that
        #      gapped sharply against the trade overnight, or shrink it on a
        #      moderate adverse gap.  Off by default and fail-open.
        gap = self.gap_filter.check(sig)
        if not gap.allowed:
            bound_log.info("engine.rejected", gate="gap_filter", reason=gap.reason)
            self.rejected_logger.log_rejection(sig, "gap_filter", gap.reason)
            return False
        if gap.action == "resize" and gap.size_modifier < 1.0:
            resized = int(order.quantity * gap.size_modifier)
            if resized <= 0:
                bound_log.info("engine.rejected", gate="gap_filter", reason="resize_to_zero")
                self.rejected_logger.log_rejection(
                    sig, "gap_filter", f"{gap.reason} (rounded to zero shares)"
                )
                return False
            bound_log.info(
                "engine.gap_resize", reason=gap.reason,
                from_qty=order.quantity, to_qty=resized,
            )
            order.quantity = resized
            sig.raw_data["gap_size_modifier"] = gap.size_modifier

        # (f) Freshness check (signal age + price drift).
        fresh, reason = self.freshness_check(sig)
        if not fresh:
            bound_log.info("engine.rejected", gate="freshness_check", reason=reason)
            self.rejected_logger.log_rejection(sig, "freshness_check", reason)
            return False

        # (g) Cash availability check.
        currency = order.currency
        available_cash = self.risk_manager.get_available_cash(currency)
        required_cash = sig.entry_price * order.quantity
        if required_cash > available_cash:
            bound_log.info(
                "engine.rejected",
                gate="cash_check",
                required=round(required_cash, 2),
                available=round(available_cash, 2),
                currency=currency,
            )
            self.rejected_logger.log_rejection(
                sig,
                "cash_check",
                f"need {required_cash:.2f} {currency}, have {available_cash:.2f}",
            )
            return False

        # Capture the trade rationale (F9) now that every gate has passed —
        # this is the exact context the decision was made with.
        rationale = self._build_rationale(sig, decision)

        # (h) Place the entry.  The order type (immediate bracket, scale-in
        #     tranches, or market-on-close) is selected from settings; scale-in
        #     and MOC rest until filled and are reconciled asynchronously.
        pending, accepted = self._place_entry(
            sig, order, currency, bound_log, rationale
        )
        if pending:
            return accepted  # journaling/registration happens on fill
        if not accepted:
            return False

        await self.notifier.notify_entry(order, order.signal.entry_price)
        action = "Shorted" if sig.direction.lower() == "short" else "Bought"
        self._push_notify(
            f"Entry: {sig.symbol}",
            f"{action} {order.quantity} {sig.symbol} @ {order.signal.entry_price:.2f} "
            f"({sig.strategy}, grade {sig.grade.value})",
        )
        return True

    async def _reflect_on_exits(self, events) -> None:
        """Write a learnings.jsonl lesson for each closed trade (best-effort).

        Runs after exit management.  Reads the just-updated journal, finds each
        event's closed row, and asks the reflection engine to record a lesson.
        Partial-takes are skipped (the runner's final close is reflected on
        instead).  Every failure is swallowed — reflection must never break the
        cycle or influence the trade that triggered it.
        """
        try:
            from analytics.performance import load_completed_trades

            trades = load_completed_trades(self.trade_logger.csv_path)
        except Exception:  # noqa: BLE001
            log.debug("engine.reflection_load_failed", exc_info=True)
            return
        if trades is None or trades.empty:
            return

        from signals.signal_types import ExitReason

        seen: set[str] = set()
        for event in events:
            symbol = getattr(event, "symbol", "")
            try:
                if event.exit_reason == ExitReason.PARTIAL_TAKE:
                    continue
                if symbol in seen:
                    continue
                seen.add(symbol)
                row = self._latest_closed_row(trades, symbol)
                if row is None:
                    continue
                await self.reflection_engine.reflect(row, all_trades=trades)
            except Exception:  # noqa: BLE001 -- never break the loop
                log.debug("engine.reflection_failed", symbol=symbol, exc_info=True)

    @staticmethod
    def _latest_closed_row(trades, symbol: str):
        """Return the most-recently-closed journal row for *symbol* as a dict."""
        sub = trades[trades["symbol"] == symbol]
        if sub.empty:
            return None
        try:
            idx = sub["exit_time"].astype(str).sort_values().index[-1]
        except Exception:  # noqa: BLE001
            idx = sub.index[-1]
        return sub.loc[idx].to_dict()

    def _on_rejection_activity(self, sig, reason: str, detail: str) -> None:
        """Mirror every gate rejection into the activity feed (F4)."""
        self.activity.log(
            "signal_rejected",
            cycle_id=self._cycle_id,
            symbol=getattr(sig, "symbol", ""),
            strategy=getattr(sig, "strategy", ""),
            gate=reason,
            reason=str(detail)[:300],
        )

    async def _check_proximity_alerts(self) -> None:
        """Alert once per symbol per day when price nears a stop or target.

        Uses the (cached) current price per open position; entirely
        best-effort — a data failure just skips the symbol this cycle.
        """
        try:
            threshold = float(self.settings.POSITION_PROXIMITY_ALERT_PCT)
            if threshold <= 0:
                return
            day = datetime.now(tz=ET).date().isoformat()
            for symbol, pos in self.risk_manager.get_open_positions().items():
                try:
                    price = fetch_current_price(symbol)
                    if not price:
                        continue
                    price = float(price)
                    stop = float(pos.get("stop_price", 0) or 0)
                    target = float(pos.get("target_price", 0) or 0)
                    if stop > 0 and abs(price - stop) / price * 100.0 <= threshold:
                        await self.notifier.notify_proximity(
                            symbol, "approaching_stop", price, stop, day
                        )
                    elif target > 0 and abs(target - price) / price * 100.0 <= threshold:
                        await self.notifier.notify_proximity(
                            symbol, "approaching_target", price, target, day
                        )
                except Exception:  # noqa: BLE001 -- per-symbol best-effort
                    continue
        except Exception:  # noqa: BLE001 -- alerts must never break the loop
            log.debug("engine.proximity_check_failed", exc_info=True)

    def _build_rationale(self, sig: Signal, decision) -> dict:
        """Assemble the F9 rationale payload (criteria + bar snapshot).

        Called once per accepted signal, after every gate has passed and
        before order placement, so it captures exactly the context the trade
        decision was made with.  Best-effort: returns minimal data on error.
        """
        from journal.rationale import build_trade_rationale, snapshot_bars

        regime_mult = None
        try:
            from analytics.regime import multiplier_for_strategy

            regime_mult = multiplier_for_strategy(sig.strategy, self._regime)
        except Exception:  # noqa: BLE001
            regime_mult = None
        try:
            criteria = build_trade_rationale(
                sig,
                ai_decision=decision,
                regime=self._regime,
                regime_multiplier=regime_mult,
                risk_reward_min=self.settings.RISK_REWARD_MIN,
            )
        except Exception:  # noqa: BLE001 -- rationale must never block entry
            log.debug("engine.rationale_build_failed", exc_info=True)
            criteria = []
        # TA1: the scorer attached the full indicator snapshot (series +
        # S/R levels + state) computed from the exact df it scored.  Its
        # bars are used verbatim so candles and series stay bar-aligned;
        # only v1 records fall back to a fresh snapshot_bars() fetch.
        indicators = None
        bars = []
        try:
            indicators = sig.raw_data.get("indicators") or None
            if indicators and indicators.get("bars"):
                bars = indicators["bars"]
        except Exception:  # noqa: BLE001
            indicators = None
        if not bars:
            try:
                bars = snapshot_bars(sig.symbol)
            except Exception:  # noqa: BLE001
                bars = []
        return {"criteria": criteria, "bars": bars, "indicators": indicators}

    def _record_rationale(
        self, sig: Signal, rationale: dict, quantity: int, fill_price: float
    ) -> None:
        """Persist the rationale for a filled entry (best-effort)."""
        self.rationale_store.record(
            sig,
            rationale.get("criteria", []),
            quantity=quantity,
            entry_price=fill_price,
            entry_time=datetime.now(tz=ET).isoformat(),
            bars=rationale.get("bars", []),
            indicators=rationale.get("indicators"),
        )

    def _push_notify(self, title: str, body: str) -> None:
        """Enqueue a PWA push notification (feature 17).  Best-effort."""
        try:
            from dashboard.push import publish

            publish(title, body, self.settings.DATA_DIR)
        except Exception:  # noqa: BLE001 -- notifications must never break trading
            pass

    # ------------------------------------------------------------------
    # Order-type selection
    # ------------------------------------------------------------------

    def _place_entry(self, sig, order, currency, bound_log, rationale=None):
        """Place an entry using the configured order type.

        Returns ``(pending, accepted)``.  For immediate brackets *pending* is
        ``False`` and *accepted* reflects the fill; for resting orders
        (scale-in / MOC) *pending* is ``True`` and *accepted* reports whether
        the order was submitted.

        *rationale* is the F9 payload built after the gates passed; it is
        persisted on fill (immediately for brackets, at reconciliation for
        resting orders).
        """
        rationale = rationale or {"criteria": [], "bars": []}
        pt_pct = (
            self.settings.PARTIAL_TAKE_PCT if self.settings.ENABLE_PARTIAL_TAKE else 0.0
        )
        pt_r = self.settings.PARTIAL_TAKE_TARGET_R
        is_short = sig.direction.lower() == "short"

        # Market-on-close: used when we are inside the cutoff window.
        # Long-only path — the resting-order fill logic assumes a buy, so
        # shorts always take the immediate bracket below.
        if (
            self.settings.ENABLE_MOC_ENTRIES
            and not is_short
            and self._is_past_order_cutoff()
        ):
            res = self.broker.place_moc_order(
                sig.symbol, order.quantity, sig.stop_price, sig.target_price,
                currency=currency, partial_take_pct=pt_pct, partial_take_target_r=pt_r,
            )
            if res.accepted:
                self._pending_orders[res.order_id] = order
                self._pending_rationale[res.order_id] = rationale
                bound_log.info("engine.moc_submitted", order_id=res.order_id)
                self.activity.log(
                    "entry_pending", cycle_id=self._cycle_id, symbol=sig.symbol,
                    strategy=sig.strategy, order_type="moc",
                )
            else:
                self.rejected_logger.log_rejection(sig, "broker", res.reason)
            return True, res.accepted

        # Scale-in: split into tranches at successively lower limit prices.
        # Long-only path (a resting limit that fills when the market trades
        # AT OR BELOW it models a buy); shorts use the immediate bracket.
        if self.settings.ENABLE_SCALE_IN and not is_short:
            from execution.advanced_orders import compute_scale_in_tranches

            tranches = compute_scale_in_tranches(
                sig.entry_price,
                order.quantity,
                self.settings.SCALE_IN_TRANCHES,
                self.settings.SCALE_IN_STEP_PCT,
            )
            results = self.broker.place_scale_in(
                sig.symbol, tranches, sig.stop_price, sig.target_price,
                currency=currency, expiry_hours=self.settings.LIMIT_ORDER_EXPIRY_HOURS,
                partial_take_pct=pt_pct, partial_take_target_r=pt_r,
            )
            any_accepted = False
            for res in results:
                if res.accepted:
                    self._pending_orders[res.order_id] = order
                    self._pending_rationale[res.order_id] = rationale
                    any_accepted = True
            bound_log.info(
                "engine.scale_in_submitted",
                tranches=len(tranches),
                accepted=any_accepted,
            )
            if any_accepted:
                self.activity.log(
                    "entry_pending", cycle_id=self._cycle_id, symbol=sig.symbol,
                    strategy=sig.strategy, order_type="scale_in",
                    tranches=len(tranches),
                )
            return True, any_accepted

        # Default: immediate bracket order (side-aware: short sale for
        # short_strategies signals).
        result = self.broker.place_bracket_order(
            symbol=sig.symbol,
            quantity=order.quantity,
            entry_price=sig.entry_price,
            stop_price=sig.stop_price,
            target_price=sig.target_price,
            currency=currency,
            partial_take_pct=pt_pct,
            partial_take_target_r=pt_r,
            side=sig.direction,
        )
        if not result.accepted:
            bound_log.warning(
                "engine.order_rejected", gate="broker", reason=result.reason
            )
            self.rejected_logger.log_rejection(sig, "broker", result.reason)
            return False, False

        self.trade_logger.log_entry(order, result.fill_price, result.commission)
        self.risk_manager.register_position(order, result.fill_price)
        self._record_rationale(sig, rationale, order.quantity, result.fill_price)
        bound_log.info(
            "engine.trade_placed",
            quantity=order.quantity,
            fill_price=result.fill_price,
            stop_price=sig.stop_price,
            target_price=sig.target_price,
            order_id=result.order_id,
        )
        self.activity.log(
            "trade_placed",
            cycle_id=self._cycle_id,
            symbol=sig.symbol,
            strategy=sig.strategy,
            grade=sig.grade.value,
            quantity=order.quantity,
            fill_price=round(result.fill_price, 4),
            stop_price=round(sig.stop_price, 4),
            target_price=round(sig.target_price, 4),
        )
        return False, True

    async def _check_risk_alerts(self) -> None:
        """Feed the alert manager's drawdown + daily-loss threshold monitors."""
        from pathlib import Path

        from analytics.performance import analyze_journal

        report = analyze_journal(
            Path(self.settings.DATA_DIR) / "trades.csv", self.settings.TOTAL_CAPITAL
        )
        curve = report.equity_curve
        if curve:
            equities = [self.settings.TOTAL_CAPITAL] + [p["equity"] for p in curve]
            peak = max(equities)
            current = equities[-1]
            drawdown = (peak - current) / peak if peak > 0 else 0.0
            await self.notifier.check_drawdown(drawdown)

        today = datetime.now(tz=ET).date().isoformat()
        await self.notifier.check_daily_loss(
            self.risk_manager.daily_pnl, self.settings.TOTAL_CAPITAL, day=today
        )

    # ------------------------------------------------------------------
    # Regime detection + adaptive auto-tuning (features 12, 13)
    # ------------------------------------------------------------------

    def _refresh_regime_and_autotune(self) -> None:
        """Recompute the market regime and adaptive thresholds for this cycle."""
        try:
            from analytics.regime import current_regime

            self._regime = current_regime(self.settings)
        except Exception:  # noqa: BLE001 -- keep the previous (or neutral) regime
            pass
        try:
            from automation.autotune import tune_from_journal

            self._autotune = tune_from_journal(self.settings)
            if self._autotune.applied:
                log.info(
                    "engine.autotune",
                    win_rate=self._autotune.win_rate,
                    delta=self._autotune.delta,
                    thresholds=self._autotune.thresholds,
                )
        except Exception:  # noqa: BLE001
            pass

    def _regime_autotune_gate(self, sig) -> Tuple[bool, str]:
        """Reject weaker (grade-B) signals when regime / auto-tune disfavour them.

        * Regime: when the strategy family's regime multiplier is below 0.8
          (bear/high-vol), only grade-A setups are taken.
        * Auto-tune: when a cold streak has raised the adaptive bar, the
          signal's strength must clear the tuned grade-B threshold.
        """
        from analytics.regime import multiplier_for_strategy
        from signals.signal_types import Grade

        if self.settings.REGIME_DETECTION_ENABLED:
            mult = multiplier_for_strategy(sig.strategy, self._regime)
            if mult < 0.8 and sig.grade != Grade.A:
                return False, (
                    f"{self._regime.regime} regime (x{mult:.2f}) — grade "
                    f"{sig.grade.value} below required A"
                )

        if self.settings.AUTOTUNE_ENABLED and self._autotune.applied:
            floor = self._autotune.thresholds.get("B")
            if floor is not None and sig.signal_strength < floor:
                return False, (
                    f"auto-tune floor {floor:.3f} > strength "
                    f"{sig.signal_strength:.3f}"
                )
        return True, "ok"

    async def _reconcile_pending_entries(self) -> int:
        """Journal + register filled resting entries; drop expired ones.

        Called at the start of each cycle.  A fill looks up its originating
        :class:`TradeOrder` (stored when the order was placed) and registers the
        position at the actual fill price and quantity; scale-in tranches are
        averaged into one position by the risk manager.
        """
        fills = self.broker.poll_pending_entries()
        registered = 0
        for fill in fills:
            order = self._pending_orders.get(fill.order_id)
            if fill.expired:
                self._pending_orders.pop(fill.order_id, None)
                self._pending_rationale.pop(fill.order_id, None)
                log.info("engine.entry_expired", order_id=fill.order_id,
                         symbol=fill.symbol)
                self.activity.log(
                    "entry_expired", cycle_id=self._cycle_id, symbol=fill.symbol,
                )
                await self.notifier.send(
                    f"⌛ Entry order for {fill.symbol} expired unfilled."
                )
                continue
            if not fill.filled or order is None:
                continue
            self._pending_orders.pop(fill.order_id, None)
            rationale = self._pending_rationale.pop(fill.order_id, None)
            self.trade_logger.log_entry(order, fill.fill_price, fill.commission,
                                        quantity=fill.quantity)
            self.risk_manager.register_position(order, fill.fill_price,
                                                quantity=fill.quantity)
            if rationale is not None:
                self._record_rationale(
                    order.signal, rationale, fill.quantity, fill.fill_price
                )
            registered += 1
            log.info("engine.pending_entry_filled", symbol=fill.symbol,
                     quantity=fill.quantity, fill_price=fill.fill_price)
            self.activity.log(
                "entry_filled", cycle_id=self._cycle_id, symbol=fill.symbol,
                quantity=fill.quantity, fill_price=round(fill.fill_price, 4),
            )
            await self.notifier.notify_entry(order, fill.fill_price)
        return registered

    # ------------------------------------------------------------------
    # Broker connection management
    # ------------------------------------------------------------------

    async def _ensure_broker_connected(self) -> bool:
        """Ensure the broker session is live, reconnecting with backoff.

        If the broker reports a healthy connection this returns immediately.
        Otherwise it retries ``connect()`` up to
        ``RECONNECT_MAX_ATTEMPTS`` times with exponential backoff
        (``BASE * 2**(attempt-1)`` seconds, capped at ``MAX_DELAY``),
        disconnecting first to clear any half-open session.

        Returns:
            ``True`` once connected, ``False`` if every attempt failed.
        """
        if self.broker.is_connected():
            return True

        log.warning("engine.broker_disconnected", broker=self.settings.BROKER)

        max_attempts = self.settings.RECONNECT_MAX_ATTEMPTS
        base_delay = self.settings.RECONNECT_BASE_DELAY_SECONDS
        max_delay = self.settings.RECONNECT_MAX_DELAY_SECONDS

        for attempt in range(1, max_attempts + 1):
            # Clear any half-open session before retrying.
            try:
                self.broker.disconnect()
            except Exception:  # noqa: BLE001 -- disconnect must never raise up
                pass

            connected = False
            try:
                connected = self.broker.connect()
            except Exception:  # noqa: BLE001 -- treat as a failed attempt
                log.exception("engine.broker_reconnect_error", attempt=attempt)

            if connected and self.broker.is_connected():
                log.info("engine.broker_reconnected", attempt=attempt)
                await self.notifier.send(
                    f"🟢 Broker reconnected after {attempt} attempt(s)."
                )
                return True

            if attempt < max_attempts:
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                log.warning(
                    "engine.broker_reconnect_retry",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    next_retry_seconds=delay,
                )
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return False

        log.error("engine.broker_reconnect_failed", attempts=max_attempts)
        await self.notifier.send(
            f"🔴 Broker reconnection FAILED after {max_attempts} attempts. "
            "Trading is halted until the connection recovers."
        )
        return False

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
        now = datetime.now(tz=ET)

        # 1. Age check — ensure both sides are tz-aware for the delta.
        sig_ts = sig.timestamp
        if sig_ts.tzinfo is None:
            sig_ts = sig_ts.replace(tzinfo=ET)
        age = now - sig_ts
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

    # ------------------------------------------------------------------
    # Mode-switch restart
    # ------------------------------------------------------------------

    def _restart_requested(self) -> bool:
        """Return whether the dashboard requested a restart (mode switch)."""
        from dashboard.mode_control import consume_restart_request

        return consume_restart_request(self.settings.DATA_DIR)

    def _restart_process(self) -> None:
        """Re-exec the engine so the new ``.env`` broker settings take effect."""
        import os

        log.info("engine.restarting_for_mode_switch")
        try:
            self.broker.disconnect()
        except Exception:  # noqa: BLE001
            pass
        # Drop the cached settings singleton so the new process reads fresh .env.
        try:
            get_settings.cache_clear()
        except Exception:  # noqa: BLE001
            pass
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def shutdown(self) -> None:
        """Request a graceful shutdown of the trading loop.

        Sets the :attr:`running` flag to ``False`` and wakes the between-cycle
        sleep so the main loop in :meth:`run` exits promptly rather than waiting
        out the remaining sleep interval.
        """
        log.info("engine.shutdown_requested")
        self.running = False
        if self._stop_event is not None:
            self._stop_event.set()
        if self._scheduler is not None:
            self._scheduler.stop()


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
