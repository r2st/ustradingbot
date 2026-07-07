"""
Risk manager -- position sizing, pre-trade checks, and open-position tracking.

This module is the single gatekeeper between a scored ``Signal`` and an
executable ``TradeOrder``.  It enforces:

* Per-strategy position caps
* Daily loss limits
* Re-entry cooldowns (exit-reason-aware)
* R:R minimums
* Position sizing with grade modifiers

All state (open positions, exit history, daily P&L) is persisted to JSON
files under ``settings.DATA_DIR`` so the bot can resume after a restart.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import structlog

from config.settings import Settings
from config.universe import get_currency
from signals.signal_types import (
    ExitEvent,
    ExitReason,
    Grade,
    Signal,
    TradeOrder,
)


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# US Eastern timezone anchors the trading-day boundary for the daily P&L
# accumulator (the daily loss limit resets on the US market calendar day).
_ET = ZoneInfo("America/New_York")


def _trading_day() -> str:
    """Return the current US-Eastern trading date as an ISO string."""
    return datetime.now(tz=_ET).date().isoformat()


# ---------------------------------------------------------------------------
# Strategy-family helpers
# ---------------------------------------------------------------------------

_MOMENTUM_FAMILY = frozenset({"momentum", "vcp_breakout"})


def _strategy_family(strategy: str) -> str:
    """Normalise a strategy name to its position-cap family."""
    s = strategy.lower()
    if s in _MOMENTUM_FAMILY:
        return "momentum"
    return s


# ---------------------------------------------------------------------------
# Long-cooldown exit reasons (24-hour penalty)
# ---------------------------------------------------------------------------

_LONG_COOLDOWN_REASONS: frozenset[ExitReason] = frozenset(
    {ExitReason.STOP_HIT, ExitReason.SETUP_BROKEN}
)

_LONG_COOLDOWN = timedelta(hours=24)


class RiskManager:
    """Central risk gatekeeper for the trading bot.

    Responsibilities:
        1. **Pre-check** a ``Signal`` before the (expensive) AI call.
        2. **Check strategy caps** to ensure no single strategy dominates.
        3. **Build a sized order** with grade and strategy modifiers.
        4. **Track open positions** via an atomic JSON file.
        5. **Track daily P&L** to enforce the daily loss limit.
        6. **Track exit history** to enforce re-entry cooldowns.

    All dollar-denominated amounts are in the position's settlement currency.
    """

    # ------------------------------------------------------------------ init

    def __init__(self, settings: Settings) -> None:
        """Initialise the risk manager.

        Args:
            settings: Application settings instance (provides all limits,
                capital allocations, and the ``DATA_DIR`` path).
        """
        self._settings = settings
        self._data_dir: Path = Path(settings.DATA_DIR)
        self._data_dir.mkdir(parents=True, exist_ok=True)

        # Positions keyed by symbol
        self._positions: Dict[str, Dict[str, Any]] = self._load_positions()

        # Daily P&L accumulator, persisted to disk so it survives a mid-day
        # restart.  It is tagged with the trading day it belongs to and reset
        # automatically when the day rolls over (see ``maybe_reset_daily_pnl``).
        self._daily_pnl, self._pnl_date = self._load_daily_pnl()

        # Exit history: list of dicts with symbol, exit_reason, exit_ts
        self._exit_history: List[Dict[str, Any]] = self._load_exit_history()

        self._log = log.bind(component="RiskManager")
        self._log.info(
            "risk_manager.initialised",
            open_positions=len(self._positions),
            exit_history_entries=len(self._exit_history),
            daily_pnl=round(self._daily_pnl, 2),
            pnl_date=self._pnl_date,
        )

    # -------------------------------------------------------------- pre_check

    def pre_check(self, signal: Signal) -> Tuple[bool, str]:
        """Run structural checks on *signal* before the AI evaluation.

        These checks are free (no API calls) and eliminate clearly
        invalid or currently blocked signals early.

        Args:
            signal: The candidate signal to evaluate.

        Returns:
            A ``(passed, reason)`` tuple.  ``passed`` is ``True`` when all
            checks pass; otherwise ``reason`` explains the first failure.
        """
        symbol = signal.symbol
        strategy = signal.strategy

        # Roll the daily P&L accumulator over if we have crossed into a new
        # trading day, so a prior day's loss can never block today's entries.
        self.maybe_reset_daily_pnl()

        # (a) Already holding same symbol
        if symbol in self._positions:
            return False, f"already_holding:{symbol}"

        # (b) Re-entry cooldown
        cooldown_remaining = self._get_cooldown_remaining(symbol)
        if cooldown_remaining is not None:
            minutes_left = int(cooldown_remaining.total_seconds() / 60)
            return False, f"reentry_cooldown:{symbol}:{minutes_left}m_remaining"

        # (c) Daily loss limit
        daily_limit = self._settings.TOTAL_CAPITAL * self._settings.DAILY_LOSS_LIMIT_PCT
        if self._daily_pnl <= -daily_limit:
            return False, (
                f"daily_loss_limit_reached:pnl={self._daily_pnl:.2f},"
                f"limit=-{daily_limit:.2f}"
            )

        # (d) Max total positions
        if len(self._positions) >= self._settings.MAX_OPEN_POSITIONS:
            return False, (
                f"max_positions_reached:{len(self._positions)}"
                f"/{self._settings.MAX_OPEN_POSITIONS}"
            )

        # (e) Invalid stop (stop >= entry)
        if signal.stop_price >= signal.entry_price:
            return False, (
                f"invalid_stop:stop={signal.stop_price:.4f}"
                f">=entry={signal.entry_price:.4f}"
            )

        # (f) Invalid target (target <= entry)
        if signal.target_price <= signal.entry_price:
            return False, (
                f"invalid_target:target={signal.target_price:.4f}"
                f"<=entry={signal.entry_price:.4f}"
            )

        # (g) Risk/reward ratio
        rr = signal.risk_reward_ratio
        if rr < self._settings.RISK_REWARD_MIN:
            return False, (
                f"rr_too_low:{rr:.2f}<{self._settings.RISK_REWARD_MIN}"
            )

        self._log.debug(
            "pre_check.passed",
            symbol=symbol,
            strategy=strategy,
            rr=round(rr, 2),
        )
        return True, "passed"

    # -------------------------------------------------------- strategy cap

    def check_strategy_cap(self, strategy: str) -> Tuple[bool, str]:
        """Check whether the per-strategy position limit has been reached.

        Strategy families and their limits:
            * ``momentum`` + ``vcp_breakout`` share ``MAX_MOMENTUM_POSITIONS``
            * ``swing`` uses ``MAX_SWING_POSITIONS``
            * ``pead`` uses ``MAX_PEAD_POSITIONS``
            * ``mean_reversion`` has a hard limit of 1

        Args:
            strategy: Strategy name (case-insensitive).

        Returns:
            ``(True, "passed")`` when there is room, or
            ``(False, "reason")`` when the cap is reached.
        """
        strategy_lower = strategy.lower()
        family = _strategy_family(strategy_lower)

        # Count open positions belonging to this family
        count = sum(
            1
            for pos in self._positions.values()
            if _strategy_family(pos.get("strategy", "")) == family
        )

        if family == "momentum":
            cap = self._settings.MAX_MOMENTUM_POSITIONS
        elif family == "swing":
            cap = self._settings.MAX_SWING_POSITIONS
        elif family == "pead":
            cap = self._settings.MAX_PEAD_POSITIONS
        elif family == "mean_reversion":
            cap = 1
        else:
            # Unknown strategy -- allow but log a warning
            self._log.warning("strategy_cap.unknown_strategy", strategy=strategy)
            return True, "passed"

        if count >= cap:
            return False, f"strategy_cap_reached:{family}:{count}/{cap}"

        return True, "passed"

    # -------------------------------------------------------- build_order

    def build_order(
        self,
        signal: Signal,
        ai_decision: str,
        ai_reasoning: str,
        ai_cost: float,
    ) -> Optional[TradeOrder]:
        """Size a trade and return a ``TradeOrder``, or ``None`` if too small.

        Position sizing logic:
            1. ``max_risk_dollars = capital_pool * MAX_POSITION_SIZE_PCT
               * ai_size_modifier * strategy_modifier``
            2. ``risk_per_share = entry - stop``
            3. ``shares = int(max_risk_dollars / risk_per_share)``
            4. Cap shares so notional <= 10 % of capital pool.
            5. Apply grade modifier (Grade.B -> 75 %).
            6. If ``final_shares == 0``, return ``None``.

        Args:
            signal: The scored and AI-approved signal.
            ai_decision: AI layer verdict (``"APPROVE"`` / ``"REJECT"``).
            ai_reasoning: AI layer free-text explanation.
            ai_cost: Estimated API cost in USD for the AI evaluation.

        Returns:
            A fully populated ``TradeOrder``, or ``None`` when sizing
            results in zero shares (no 1-share floor).
        """
        currency = get_currency(signal.symbol)
        capital_pool = self._settings.get_capital_for_currency(currency)

        # Size modifier from AI (future hook; currently always 1.0)
        ai_size_modifier = 1.0

        # Strategy modifier
        strategy_modifier = 0.5 if signal.strategy.lower() == "mean_reversion" else 1.0

        max_risk_dollars = (
            capital_pool
            * self._settings.MAX_POSITION_SIZE_PCT
            * ai_size_modifier
            * strategy_modifier
        )

        risk_per_share = signal.entry_price - signal.stop_price
        if risk_per_share <= 0:
            self._log.warning(
                "build_order.non_positive_risk",
                symbol=signal.symbol,
                risk_per_share=risk_per_share,
            )
            return None

        shares = int(max_risk_dollars / risk_per_share)

        # Notional cap: no single position exceeds 10 % of capital pool
        if signal.entry_price > 0:
            notional_cap = int(capital_pool * 0.10 / signal.entry_price)
            shares = min(shares, notional_cap)

        # Grade modifier
        if signal.grade == Grade.B:
            shares = int(shares * 0.75)

        if shares == 0:
            self._log.info(
                "build_order.zero_shares",
                symbol=signal.symbol,
                max_risk_dollars=round(max_risk_dollars, 2),
                risk_per_share=round(risk_per_share, 4),
            )
            return None

        risk_amount = risk_per_share * shares

        order = TradeOrder(
            signal=signal,
            quantity=shares,
            risk_amount=round(risk_amount, 2),
            max_risk_dollars=round(max_risk_dollars, 2),
            currency=currency,
            ai_decision=ai_decision,
            ai_reasoning=ai_reasoning,
            ai_cost_usd=round(ai_cost, 6),
        )

        self._log.info(
            "build_order.created",
            symbol=signal.symbol,
            shares=shares,
            risk_amount=round(risk_amount, 2),
            max_risk_dollars=round(max_risk_dollars, 2),
            grade=signal.grade.value,
            strategy=signal.strategy,
        )
        return order

    # -------------------------------------------------------- validate_stop

    def validate_stop(
        self,
        stop: float,
        entry: float,
        current_price: Optional[float] = None,
    ) -> float:
        """Validate and clamp a stop-loss price.

        Rules applied in order:
            1. Stop must be strictly below entry; if not, clamp to
               ``entry * 0.93`` (7 % trailing stop fallback).
            2. If *current_price* is known, stop must be below it;
               if not, clamp to ``current_price * 0.985`` (1.5 % cushion).

        Args:
            stop: Proposed stop-loss price.
            entry: Entry (limit buy) price.
            current_price: Current market price, if available.

        Returns:
            The validated stop price (may be the original or a clamped value).
        """
        validated = stop

        # Rule 1: stop < entry
        if validated >= entry:
            validated = round(entry * 0.93, 4)
            self._log.warning(
                "validate_stop.clamped_to_entry",
                original_stop=stop,
                entry=entry,
                new_stop=validated,
            )

        # Rule 2: stop < current_price
        if current_price is not None and validated >= current_price:
            validated = round(current_price * 0.985, 4)
            self._log.warning(
                "validate_stop.clamped_to_current",
                original_stop=stop,
                current_price=current_price,
                new_stop=validated,
            )

        return validated

    # -------------------------------------------------- position management

    def register_position(
        self,
        order: TradeOrder,
        fill_price: float,
        quantity: Optional[int] = None,
    ) -> None:
        """Record a newly opened position, or average into an existing one.

        When *quantity* is given it overrides ``order.quantity`` (used for a
        scale-in tranche that fills for only part of the intended size).  If a
        position already exists for the symbol, the new fill is averaged into
        it (weighted-average entry, summed quantity) rather than overwriting —
        this is how scale-in tranches accumulate into a single position.

        Args:
            order: The executed ``TradeOrder``.
            fill_price: Actual fill price from the broker.
            quantity: Optional share count for this fill (defaults to the
                order's quantity).
        """
        symbol = order.signal.symbol
        qty = int(order.quantity if quantity is None else quantity)

        existing = self._positions.get(symbol)
        if existing is not None:
            prev_qty = int(existing.get("quantity", 0) or 0)
            prev_entry = float(existing.get("entry_price", 0) or 0)
            total_qty = prev_qty + qty
            if total_qty > 0:
                existing["entry_price"] = round(
                    (prev_entry * prev_qty + fill_price * qty) / total_qty, 4
                )
            existing["quantity"] = total_qty
            existing["risk_amount"] = round(
                float(existing.get("risk_amount", 0) or 0) + order.risk_amount, 2
            )
            self._save_positions()
            self._log.info(
                "position.averaged_in",
                symbol=symbol,
                added_qty=qty,
                total_qty=total_qty,
                avg_entry=existing["entry_price"],
            )
            return

        self._positions[symbol] = {
            "symbol": symbol,
            "strategy": order.signal.strategy,
            # "long" for every automated entry; manual sells record "short".
            "direction": order.signal.direction,
            # Manual dashboard entries are flagged so the automated exit
            # sweeps (time / health / trailing) leave them to their
            # operator-defined exit ladder (kept in "levels").
            "manual": bool(order.signal.raw_data.get("manual", False)),
            "levels": list(order.signal.raw_data.get("levels", []) or []),
            "entry_price": fill_price,
            "stop_price": order.signal.stop_price,
            # Preserve the entry-time stop so dynamic-stop R-multiple and
            # drawdown calculations measure risk from the original level even
            # after the live stop has been ratcheted up.
            "original_stop_loss": order.signal.stop_price,
            "target_price": order.signal.target_price,
            "quantity": qty,
            "currency": order.currency,
            "risk_amount": order.risk_amount,
            "grade": order.signal.grade.value,
            "signal_strength": order.signal.signal_strength,
            "ai_decision": order.ai_decision,
            "ai_reasoning": order.ai_reasoning,
            "ai_cost_usd": order.ai_cost_usd,
            "entry_time": datetime.now().isoformat(),
        }
        self._save_positions()
        self._log.info(
            "position.registered",
            symbol=symbol,
            fill_price=fill_price,
            quantity=qty,
            strategy=order.signal.strategy,
        )

    def reduce_position(self, symbol: str, quantity: int) -> bool:
        """Shrink an open position by *quantity* shares (partial profit-taking).

        Used when a ``PARTIAL_TAKE`` event trims part of a position while the
        remainder keeps running.  The position is *not* removed and no exit is
        recorded for cooldown purposes.

        Returns:
            ``True`` if the position existed and was reduced, else ``False``.
        """
        pos = self._positions.get(symbol)
        if pos is None:
            return False
        remaining = int(pos.get("quantity", 0) or 0) - int(quantity)
        pos["quantity"] = max(0, remaining)
        self._save_positions()
        self._log.info(
            "position.reduced", symbol=symbol, sold=quantity, remaining=pos["quantity"]
        )
        return True

    def remove_position(self, symbol: str, exit_event: ExitEvent) -> None:
        """Remove a closed position and record the exit for cooldown tracking.

        Args:
            symbol: Ticker symbol of the closed position.
            exit_event: Details of how and why the position was closed.
        """
        removed = self._positions.pop(symbol, None)
        if removed is None:
            self._log.warning("position.remove_not_found", symbol=symbol)

        # Record exit for cooldown tracking
        self._exit_history.append(
            {
                "symbol": symbol,
                "exit_reason": exit_event.exit_reason.value,
                "exit_ts": (
                    exit_event.exit_date or datetime.now()
                ).isoformat(),
                "exit_price": exit_event.exit_price,
                "pnl_gross": exit_event.pnl_gross,
            }
        )
        self._save_exit_history()
        self._save_positions()
        self._log.info(
            "position.removed",
            symbol=symbol,
            exit_reason=exit_event.exit_reason.value,
            pnl_gross=round(exit_event.pnl_gross, 2),
        )

    def update_stop(self, symbol: str, new_stop: float) -> bool:
        """Update the stored stop for an open position (trailing stops).

        Only mutates the mutable ``stop_price``; ``original_stop_loss`` (set
        at entry) is preserved for drawdown calculations.

        Args:
            symbol: Ticker symbol of the open position.
            new_stop: The new (higher) stop price.

        Returns:
            ``True`` if the position existed and was updated, else ``False``.
        """
        pos = self._positions.get(symbol)
        if pos is None:
            return False
        pos.setdefault("original_stop_loss", pos.get("stop_price"))
        pos["stop_price"] = round(new_stop, 4)
        self._save_positions()
        self._log.info("position.stop_updated", symbol=symbol, new_stop=new_stop)
        return True

    def get_open_positions(self) -> Dict[str, Dict[str, Any]]:
        """Return a copy of the current open positions.

        Returns:
            Dict keyed by symbol, each value a dict of position attributes.
        """
        return dict(self._positions)

    def get_available_cash(self, currency: str) -> float:
        """Return uncommitted capital for *currency*.

        Available cash = allocated capital minus the sum of
        ``entry_price * quantity`` for all open positions in that currency.

        Args:
            currency: ISO 4217 currency code (``"USD"`` or ``"CAD"``).

        Returns:
            Available cash in the given currency (may be negative if
            positions exceed allocation, which should not happen in
            normal operation).
        """
        total = self._settings.get_capital_for_currency(currency)
        committed = sum(
            pos["entry_price"] * pos["quantity"]
            for pos in self._positions.values()
            if pos.get("currency", "USD") == currency.upper()
        )
        return total - committed

    # ------------------------------------------------------- daily P&L

    @property
    def daily_pnl(self) -> float:
        """Current accumulated P&L for today's trading session (dollars)."""
        return self._daily_pnl

    def record_daily_pnl(self, pnl: float) -> None:
        """Accumulate a P&L amount to the daily tracker.

        Rolls the accumulator over first if the trading day has changed, then
        adds *pnl* and persists the result so it survives a restart.

        Args:
            pnl: Dollar P&L to add (negative for losses).
        """
        self.maybe_reset_daily_pnl()
        self._daily_pnl += pnl
        self._save_daily_pnl()
        self._log.debug(
            "daily_pnl.recorded",
            pnl_delta=round(pnl, 2),
            daily_pnl=round(self._daily_pnl, 2),
        )

    def maybe_reset_daily_pnl(self) -> bool:
        """Reset the accumulator if the US trading day has rolled over.

        Called at the start of each cycle (and before every P&L read/write) so
        the daily loss limit is measured against *today* only.  Persisted state
        is updated so the reset survives a restart.

        Returns:
            ``True`` if a rollover reset occurred, ``False`` otherwise.
        """
        today = _trading_day()
        if today == self._pnl_date:
            return False
        prev = self._daily_pnl
        self._daily_pnl = 0.0
        self._pnl_date = today
        self._save_daily_pnl()
        self._log.info(
            "daily_pnl.rolled_over",
            previous=round(prev, 2),
            new_date=today,
        )
        return True

    def reset_daily_pnl(self) -> None:
        """Force-reset the daily P&L accumulator to zero for the current day.

        Call this at the start of each trading day.  Prefer
        :meth:`maybe_reset_daily_pnl` for automatic day-boundary handling.
        """
        prev = self._daily_pnl
        self._daily_pnl = 0.0
        self._pnl_date = _trading_day()
        self._save_daily_pnl()
        self._log.info("daily_pnl.reset", previous=round(prev, 2))

    # ------------------------------------------------------- private helpers

    def _load_positions(self) -> Dict[str, Dict[str, Any]]:
        """Load open positions from the persistent JSON file.

        Returns:
            Dict of positions keyed by symbol, or an empty dict if the
            file does not exist or is malformed.
        """
        path = self._data_dir / "open_positions.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                log.warning(
                    "load_positions.invalid_format",
                    path=str(path),
                    type=type(data).__name__,
                )
                return {}
            return data
        except (json.JSONDecodeError, OSError) as exc:
            log.error("load_positions.failed", path=str(path), error=str(exc))
            return {}

    def _save_positions(self) -> None:
        """Persist open positions to JSON using atomic write.

        Writes to a temporary file in the same directory, then renames
        to the target path.  This prevents corruption from crashes
        during write.
        """
        target = self._data_dir / "open_positions.json"
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._data_dir),
                prefix=".open_positions_",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self._positions, f, indent=2, default=str)
                os.replace(tmp_path, str(target))
            except BaseException:
                # Clean up the temp file on any failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as exc:
            self._log.error(
                "save_positions.failed",
                path=str(target),
                error=str(exc),
            )

    def _load_daily_pnl(self) -> Tuple[float, str]:
        """Load the persisted daily P&L, honouring the trading-day boundary.

        Returns a ``(pnl, date)`` tuple.  If the persisted record belongs to a
        previous trading day (or is missing/corrupt), the accumulator starts at
        ``0.0`` for today -- a prior day's loss is never carried forward.
        """
        today = _trading_day()
        path = self._data_dir / "daily_pnl.json"
        if not path.exists():
            return 0.0, today
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            stored_date = str(data["date"])
            stored_pnl = float(data["pnl"])
        except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError) as exc:
            log.error("load_daily_pnl.failed", path=str(path), error=str(exc))
            return 0.0, today
        if stored_date != today:
            # Stale record from a previous day -- start today fresh.
            log.info(
                "load_daily_pnl.stale_reset",
                stored_date=stored_date,
                today=today,
                stored_pnl=round(stored_pnl, 2),
            )
            return 0.0, today
        return stored_pnl, stored_date

    def _save_daily_pnl(self) -> None:
        """Persist the daily P&L accumulator (with its date) via atomic write."""
        target = self._data_dir / "daily_pnl.json"
        payload = {"date": self._pnl_date, "pnl": round(self._daily_pnl, 4)}
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._data_dir),
                prefix=".daily_pnl_",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
                os.replace(tmp_path, str(target))
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as exc:
            self._log.error(
                "save_daily_pnl.failed",
                path=str(target),
                error=str(exc),
            )

    def _load_exit_history(self) -> List[Dict[str, Any]]:
        """Load exit history from the persistent JSON file.

        Returns:
            List of exit records, or an empty list if the file does not
            exist or is malformed.
        """
        path = self._data_dir / "exit_history.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                log.warning(
                    "load_exit_history.invalid_format",
                    path=str(path),
                    type=type(data).__name__,
                )
                return []
            return data
        except (json.JSONDecodeError, OSError) as exc:
            log.error(
                "load_exit_history.failed", path=str(path), error=str(exc)
            )
            return []

    def _save_exit_history(self) -> None:
        """Persist exit history to JSON using atomic write."""
        target = self._data_dir / "exit_history.json"
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._data_dir),
                prefix=".exit_history_",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self._exit_history, f, indent=2, default=str)
                os.replace(tmp_path, str(target))
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as exc:
            self._log.error(
                "save_exit_history.failed",
                path=str(target),
                error=str(exc),
            )

    def _get_cooldown_remaining(self, symbol: str) -> Optional[timedelta]:
        """Check if *symbol* is still under a re-entry cooldown.

        Cooldown durations are exit-reason-aware:
            * ``STOP_HIT`` and ``SETUP_BROKEN`` incur a **24-hour** cooldown.
            * ``TARGET_HIT`` incurs a cooldown of
              ``REENTRY_COOLDOWN_MINUTES`` (default 90 min).
            * All other exit reasons: no cooldown.

        Args:
            symbol: Ticker symbol to check.

        Returns:
            The remaining cooldown as a ``timedelta`` if the symbol is
            still blocked, or ``None`` if it is free to re-enter.
        """
        now = datetime.now()

        # Walk the exit history in reverse to find the most recent exit
        # for this symbol.
        for record in reversed(self._exit_history):
            if record.get("symbol") != symbol:
                continue

            try:
                exit_reason = ExitReason(record["exit_reason"])
            except (KeyError, ValueError):
                continue

            try:
                exit_ts = datetime.fromisoformat(record["exit_ts"])
            except (KeyError, ValueError, TypeError):
                continue

            # Determine cooldown duration based on exit reason
            if exit_reason in _LONG_COOLDOWN_REASONS:
                cooldown = _LONG_COOLDOWN
            elif exit_reason == ExitReason.TARGET_HIT:
                cooldown = timedelta(
                    minutes=self._settings.REENTRY_COOLDOWN_MINUTES
                )
            else:
                # No cooldown for other exit reasons
                return None

            elapsed = now - exit_ts
            if elapsed < cooldown:
                return cooldown - elapsed

            # Cooldown has expired
            return None

        # No exit history for this symbol -- no cooldown
        return None
