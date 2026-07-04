"""
Broker abstraction and the built-in paper broker.

The trading engine talks to a broker exclusively through the small
:class:`Broker` interface defined here.  Two implementations are provided:

* :class:`PaperBroker` -- a fully simulated broker that fills bracket orders
  and detects stop/target hits from Yahoo Finance price data.  It needs no
  external gateway, so the bot runs autonomously on a headless server.  This
  is the default (``BROKER=paper``).
* :class:`IBKRBroker` -- a thin ib_insync-backed adapter for live/paper
  trading against a running Interactive Brokers TWS/Gateway.  It is optional:
  ``ib_insync`` is imported lazily so the paper path has no hard dependency.

Both brokers speak in terms of :class:`BracketResult` (the outcome of placing
an order) and produce :class:`~signals.signal_types.ExitEvent` objects when a
bracket leg fills.  The broker is the *source of truth* for whether a position
is actually open -- the risk manager's JSON tracks intent, the broker tracks
reality (mirroring the IBKR-is-source-of-truth invariant from the design doc).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

import structlog

from config.settings import Settings
from data.fetcher import fetch_current_price, fetch_ohlcv
from signals.signal_types import ExitEvent, ExitReason

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass
class BracketResult:
    """Outcome of a bracket-order placement.

    Attributes:
        accepted: Whether the broker accepted and filled the entry.
        fill_price: The entry fill price (0.0 if not filled).
        order_id: Broker order/reference id for the parent order.
        commission: Entry commission charged.
        reason: Human-readable status detail.
    """

    accepted: bool
    fill_price: float = 0.0
    order_id: str = ""
    commission: float = 0.0
    reason: str = ""


# ---------------------------------------------------------------------------
# Fill-simulation helpers (shared by PaperBroker and the backtester)
# ---------------------------------------------------------------------------
#
# These are pure functions so the paper broker and the backtest engine model
# slippage, gap-through stops, and commission identically -- a backtest and a
# live paper run of the same signal produce the same fills.


def apply_entry_slippage(entry_price: float, slippage_bps: float) -> float:
    """Return the entry fill price after adverse slippage.

    A buy fills *above* the requested price by ``slippage_bps`` basis points
    (1 bp = 0.01%).
    """
    return round(entry_price * (1.0 + slippage_bps / 10_000.0), 4)


def stop_exit_fill(stop_price: float, bar_open: float, slippage_bps: float) -> float:
    """Return the fill price for a stop that triggered on this bar.

    If the bar *gapped open below the stop*, the resting stop becomes a market
    order and fills at the (worse) open price.  Otherwise it fills at the stop
    price minus ``slippage_bps`` of adverse slippage.
    """
    if bar_open < stop_price:
        return round(bar_open, 4)
    return round(stop_price * (1.0 - slippage_bps / 10_000.0), 4)


def target_exit_fill(target_price: float, bar_open: float) -> float:
    """Return the fill price for a take-profit target that triggered.

    A resting limit sell fills at the target, or *better* if the bar gapped
    open above it (favourable slippage, so no penalty is applied).
    """
    if bar_open > target_price:
        return round(bar_open, 4)
    return round(target_price, 4)


def commission_for(quantity: int, per_share: float) -> float:
    """Return the commission charged for *quantity* shares at *per_share*."""
    return round(abs(int(quantity)) * per_share, 4)


class Broker(Protocol):
    """Minimal broker interface the engine depends on."""

    def connect(self) -> bool: ...

    def disconnect(self) -> None: ...

    def is_connected(self) -> bool:
        """Return whether the broker session is currently live."""
        ...

    def get_positions(self) -> Dict[str, int]:
        """Return live share counts keyed by symbol."""
        ...

    def place_bracket_order(
        self,
        symbol: str,
        quantity: int,
        entry_price: float,
        stop_price: float,
        target_price: float,
        currency: str = "USD",
    ) -> BracketResult: ...

    def poll_exits(self) -> List[ExitEvent]:
        """Detect and return any bracket legs (stop/target) that have filled."""

    def modify_stop(self, symbol: str, new_stop: float) -> bool: ...

    def force_close(self, symbol: str, reason: ExitReason) -> Optional[ExitEvent]:
        """Close *symbol* at market and return the resulting exit event."""
        ...


# ---------------------------------------------------------------------------
# Paper broker
# ---------------------------------------------------------------------------


@dataclass
class _PaperPosition:
    """Internal record for a simulated open position."""

    symbol: str
    quantity: int
    entry_price: float
    stop_price: float
    target_price: float
    currency: str
    order_id: str
    opened_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "currency": self.currency,
            "order_id": self.order_id,
            "opened_at": self.opened_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "_PaperPosition":
        return cls(
            symbol=d["symbol"],
            quantity=int(d["quantity"]),
            entry_price=float(d["entry_price"]),
            stop_price=float(d["stop_price"]),
            target_price=float(d["target_price"]),
            currency=d.get("currency", "USD"),
            order_id=d.get("order_id", ""),
            opened_at=d.get("opened_at", datetime.now().isoformat()),
        )


class PaperBroker:
    """Simulated broker with persistent state.

    Bracket orders fill immediately, at the requested entry price plus adverse
    slippage (``PAPER_SLIPPAGE_BPS``); a per-share commission
    (``PAPER_COMMISSION_PER_SHARE``) is charged on entry and again on exit.  On
    each :meth:`poll_exits` call the broker fetches the latest daily bar for
    every open position and closes any position whose bar low pierced the stop
    (``STOP_HIT``) or whose bar high reached the target (``TARGET_HIT``).  Stop
    fills model gap-through (a bar that opens below the stop fills at the open,
    not the stop).  When both stop and target are touched in the same bar, the
    stop is assumed to fill first (the conservative assumption).

    Reported P&L is gross; each :class:`ExitEvent` carries the exit commission
    in ``fill_details["commission"]`` so the journal can compute a net figure.

    State is persisted to ``paper_broker.json`` under ``settings.DATA_DIR`` so
    positions survive restarts.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._data_dir = Path(settings.DATA_DIR)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._data_dir / "paper_broker.json"
        self._positions: Dict[str, _PaperPosition] = self._load()
        self._order_seq = self._max_order_seq()
        self._log = log.bind(component="PaperBroker")

    # ------------------------------------------------------------- lifecycle

    def connect(self) -> bool:
        self._log.info("paper_broker.connected", open_positions=len(self._positions))
        return True

    def disconnect(self) -> None:
        self._save()

    def is_connected(self) -> bool:
        # The paper broker has no external session; it is always available.
        return True

    # ------------------------------------------------------------- positions

    def get_positions(self) -> Dict[str, int]:
        return {s: p.quantity for s, p in self._positions.items()}

    def get_position_detail(self, symbol: str) -> Optional[Dict[str, Any]]:
        pos = self._positions.get(symbol)
        return pos.to_dict() if pos else None

    # ---------------------------------------------------------- place order

    def place_bracket_order(
        self,
        symbol: str,
        quantity: int,
        entry_price: float,
        stop_price: float,
        target_price: float,
        currency: str = "USD",
    ) -> BracketResult:
        """Simulate a bracket order filling immediately with slippage + commission.

        The entry fills at *entry_price* plus ``PAPER_SLIPPAGE_BPS`` of adverse
        slippage, and a per-share commission is charged and returned on the
        :class:`BracketResult`.
        """
        if quantity <= 0:
            return BracketResult(False, reason="zero_quantity")
        if symbol in self._positions:
            return BracketResult(False, reason="already_open")

        fill_price = apply_entry_slippage(
            entry_price, self._settings.PAPER_SLIPPAGE_BPS
        )
        commission = commission_for(
            quantity, self._settings.PAPER_COMMISSION_PER_SHARE
        )

        self._order_seq += 1
        order_id = f"PAPER-{self._order_seq:06d}"
        self._positions[symbol] = _PaperPosition(
            symbol=symbol,
            quantity=quantity,
            entry_price=fill_price,
            stop_price=round(stop_price, 4),
            target_price=round(target_price, 4),
            currency=currency,
            order_id=order_id,
            opened_at=datetime.now().isoformat(),
        )
        self._save()
        self._log.info(
            "paper_broker.filled",
            symbol=symbol,
            quantity=quantity,
            entry_price=fill_price,
            slippage_bps=self._settings.PAPER_SLIPPAGE_BPS,
            commission=commission,
            order_id=order_id,
        )
        return BracketResult(
            accepted=True,
            fill_price=fill_price,
            order_id=order_id,
            commission=commission,
            reason="filled",
        )

    # ---------------------------------------------------------- poll exits

    def poll_exits(self) -> List[ExitEvent]:
        """Close any positions whose stop or target was hit on the latest bar."""
        events: List[ExitEvent] = []
        for symbol in list(self._positions.keys()):
            pos = self._positions[symbol]
            event = self._check_position_exit(pos)
            if event is not None:
                events.append(event)
                del self._positions[symbol]
        if events:
            self._save()
        return events

    def _check_position_exit(self, pos: _PaperPosition) -> Optional[ExitEvent]:
        """Return an :class:`ExitEvent` if *pos* hit its stop or target."""
        df = fetch_ohlcv(pos.symbol, period="5d")
        if df is None or df.empty:
            # Fall back to the current price as open/high/low.
            price = fetch_current_price(pos.symbol)
            if price is None:
                return None
            bar_open = bar_low = bar_high = price
        else:
            last = df.iloc[-1]
            bar_open = float(last["Open"])
            bar_low = float(last["Low"])
            bar_high = float(last["High"])

        slippage_bps = self._settings.PAPER_SLIPPAGE_BPS

        # Stop assumed to fill first when both are touched (conservative).
        # A gap-through open fills at the (worse) open price, not the stop.
        if bar_low <= pos.stop_price:
            fill = stop_exit_fill(pos.stop_price, bar_open, slippage_bps)
            return self._build_exit(pos, fill, ExitReason.STOP_HIT)
        if bar_high >= pos.target_price:
            fill = target_exit_fill(pos.target_price, bar_open)
            return self._build_exit(pos, fill, ExitReason.TARGET_HIT)
        return None

    def _build_exit(
        self, pos: _PaperPosition, exit_price: float, reason: ExitReason
    ) -> ExitEvent:
        pnl = (exit_price - pos.entry_price) * pos.quantity
        commission = commission_for(
            pos.quantity, self._settings.PAPER_COMMISSION_PER_SHARE
        )
        self._log.info(
            "paper_broker.exit",
            symbol=pos.symbol,
            reason=reason.value,
            exit_price=exit_price,
            pnl_gross=round(pnl, 2),
            commission=commission,
        )
        return ExitEvent(
            symbol=pos.symbol,
            exit_price=round(exit_price, 4),
            exit_reason=reason,
            exit_date=datetime.now(),
            pnl_gross=round(pnl, 2),
            fill_details={
                "order_id": pos.order_id,
                "quantity": pos.quantity,
                "commission": commission,
            },
        )

    # --------------------------------------------------- manual / forced exit

    def force_close(self, symbol: str, reason: ExitReason) -> Optional[ExitEvent]:
        """Close *symbol* at the current market price (used by exit manager)."""
        pos = self._positions.get(symbol)
        if pos is None:
            return None
        price = fetch_current_price(symbol) or pos.entry_price
        event = self._build_exit(pos, price, reason)
        del self._positions[symbol]
        self._save()
        return event

    def modify_stop(self, symbol: str, new_stop: float) -> bool:
        """Ratchet the stop for *symbol* (only ever moves up)."""
        pos = self._positions.get(symbol)
        if pos is None:
            return False
        if new_stop <= pos.stop_price:
            return False
        pos.stop_price = round(new_stop, 4)
        self._save()
        self._log.info("paper_broker.stop_modified", symbol=symbol, new_stop=new_stop)
        return True

    # ------------------------------------------------------------- internals

    def _max_order_seq(self) -> int:
        seq = 0
        for p in self._positions.values():
            try:
                seq = max(seq, int(p.order_id.split("-")[-1]))
            except (ValueError, IndexError):
                continue
        return seq

    def _load(self) -> Dict[str, _PaperPosition]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return {
                s: _PaperPosition.from_dict(d)
                for s, d in raw.get("positions", {}).items()
            }
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            return {}

    def _save(self) -> None:
        payload = {
            "positions": {s: p.to_dict() for s, p in self._positions.items()},
            "saved_at": datetime.now().isoformat(),
        }
        try:
            fd, tmp = tempfile.mkstemp(
                dir=str(self._data_dir), prefix=".paper_broker_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, default=str)
                os.replace(tmp, str(self._path))
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            self._log.error("paper_broker.save_failed", error=str(exc))


# ---------------------------------------------------------------------------
# IBKR broker (optional -- requires ib_insync + running TWS/Gateway)
# ---------------------------------------------------------------------------


class IBKRBroker:
    """Interactive Brokers adapter (optional).

    A thin wrapper that connects to a running TWS/Gateway via ib_insync and
    places native bracket orders.  ib_insync is imported lazily inside the
    methods that need it so importing this module never requires the package.

    **Bracket tracking.**  Every bracket order placed in a session is recorded
    in :attr:`_brackets` (keyed by symbol) together with the live ib_insync
    ``Trade`` objects for its take-profit and stop-loss legs.  This is what
    lets :meth:`poll_exits` detect when a stop or target leg fills,
    :meth:`modify_stop` ratchet the stop, and :meth:`force_close` cancel the
    resting legs before a market exit.

    **Restart caveat.**  Tracking is in-memory, so bracket legs placed in a
    previous process are not re-attached on restart.  The exchange-side bracket
    still protects the position (IBKR keeps the OCA group alive), but the bot
    will not emit an :class:`ExitEvent` for a leg that filled while it was
    down.  Cross-session reconciliation via ``reqExecutions()`` is a documented
    follow-up; for unattended deployments prefer :class:`PaperBroker`.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._ib = None
        # symbol -> bracket record (contract, quantity, prices, leg trades)
        self._brackets: Dict[str, Dict[str, Any]] = {}
        self._log = log.bind(component="IBKRBroker")

    def connect(self) -> bool:
        try:
            from ib_insync import IB  # type: ignore
        except ImportError:
            self._log.error("ibkr.ib_insync_missing")
            return False
        self._ib = IB()
        try:
            self._ib.connect(
                self._settings.IBKR_HOST,
                self._settings.IBKR_PORT,
                clientId=self._settings.IBKR_CLIENT_ID,
            )
            self._log.info("ibkr.connected", port=self._settings.IBKR_PORT)
            return True
        except Exception as exc:  # noqa: BLE001
            self._log.error("ibkr.connect_failed", error=str(exc))
            return False

    def disconnect(self) -> None:
        if self._ib is not None:
            try:
                self._ib.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def is_connected(self) -> bool:
        """Return whether the underlying ib_insync session is live."""
        if self._ib is None:
            return False
        try:
            return bool(self._ib.isConnected())
        except Exception:  # noqa: BLE001
            return False

    def get_positions(self) -> Dict[str, int]:
        if self._ib is None:
            return {}
        result: Dict[str, int] = {}
        for pos in self._ib.positions():
            result[pos.contract.symbol] = int(pos.position)
        return result

    def place_bracket_order(
        self,
        symbol: str,
        quantity: int,
        entry_price: float,
        stop_price: float,
        target_price: float,
        currency: str = "USD",
    ) -> BracketResult:
        if self._ib is None:
            return BracketResult(False, reason="not_connected")
        try:
            from ib_insync import Stock  # type: ignore

            contract = Stock(symbol, "SMART", currency)
            self._ib.qualifyContracts(contract)
            # ib_insync returns BracketOrder(parent, takeProfit, stopLoss).
            bracket = self._ib.bracketOrder(
                "BUY",
                quantity,
                limitPrice=round(entry_price * 1.002, 2),
                takeProfitPrice=round(target_price, 2),
                stopLossPrice=round(stop_price, 2),
            )
            trades = [self._ib.placeOrder(contract, order) for order in bracket]
            # Track the resting legs so we can detect fills / modify / cancel.
            self._brackets[symbol] = {
                "contract": contract,
                "quantity": int(quantity),
                "entry_price": round(entry_price, 4),
                "stop_price": round(stop_price, 2),
                "target_price": round(target_price, 2),
                "parent_trade": trades[0],
                "target_trade": trades[1] if len(trades) > 1 else None,
                "stop_trade": trades[2] if len(trades) > 2 else None,
            }
            return BracketResult(
                accepted=True,
                fill_price=entry_price,
                order_id=str(bracket[0].orderId),
                reason="submitted",
            )
        except Exception as exc:  # noqa: BLE001
            self._log.error("ibkr.place_failed", symbol=symbol, error=str(exc))
            return BracketResult(False, reason=f"error:{exc}")

    # ------------------------------------------------------------- poll exits

    def poll_exits(self) -> List[ExitEvent]:
        """Detect bracket legs that filled and emit matching exit events.

        For every tracked bracket the stop leg is checked before the target
        leg (the conservative assumption when both could have triggered).  A
        filled leg produces an :class:`ExitEvent`, the sibling (OCA) leg is
        cancelled defensively, and the bracket is dropped from tracking.
        """
        if self._ib is None:
            return []
        events: List[ExitEvent] = []
        for symbol in list(self._brackets.keys()):
            bracket = self._brackets[symbol]
            event = self._detect_bracket_fill(symbol, bracket)
            if event is not None:
                events.append(event)
                del self._brackets[symbol]
        return events

    def _detect_bracket_fill(
        self, symbol: str, bracket: Dict[str, Any]
    ) -> Optional[ExitEvent]:
        """Return an :class:`ExitEvent` if the stop or target leg has filled."""
        # (leg key, exit reason, fallback price key) — stop checked first.
        legs = (
            ("stop_trade", ExitReason.STOP_HIT, "stop_price"),
            ("target_trade", ExitReason.TARGET_HIT, "target_price"),
        )
        for leg_key, reason, price_key in legs:
            trade = bracket.get(leg_key)
            filled, fill_price = self._leg_fill(trade)
            if not filled:
                continue
            exit_price = fill_price or float(bracket[price_key])
            entry = float(bracket["entry_price"])
            qty = int(bracket["quantity"])
            self._cancel_sibling(bracket, leg_key)
            self._log.info(
                "ibkr.exit_detected",
                symbol=symbol,
                reason=reason.value,
                exit_price=exit_price,
            )
            return ExitEvent(
                symbol=symbol,
                exit_price=round(exit_price, 4),
                exit_reason=reason,
                exit_date=datetime.now(),
                pnl_gross=round((exit_price - entry) * qty, 2),
                fill_details={
                    "order_id": str(getattr(getattr(trade, "order", None), "orderId", "")),
                    "quantity": qty,
                },
            )
        return None

    @staticmethod
    def _leg_fill(trade: Any) -> tuple[bool, float]:
        """Return ``(is_filled, avg_fill_price)`` for an ib_insync ``Trade``."""
        if trade is None:
            return False, 0.0
        status = getattr(trade, "orderStatus", None)
        if status is None:
            return False, 0.0
        if getattr(status, "status", "") == "Filled":
            return True, float(getattr(status, "avgFillPrice", 0.0) or 0.0)
        return False, 0.0

    def _cancel_sibling(self, bracket: Dict[str, Any], filled_leg: str) -> None:
        """Cancel the resting sibling leg once one side of the bracket fills."""
        sibling_key = (
            "target_trade" if filled_leg == "stop_trade" else "stop_trade"
        )
        trade = bracket.get(sibling_key)
        if trade is None or self._ib is None:
            return
        try:
            self._ib.cancelOrder(trade.order)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("ibkr.cancel_sibling_failed", error=str(exc))

    # ------------------------------------------------------------- modify stop

    def modify_stop(self, symbol: str, new_stop: float) -> bool:
        """Ratchet the stop-loss leg upward (never down).

        The existing stop order is re-placed with the same ``orderId`` and a
        higher ``auxPrice``.  Re-placing (rather than cancel-then-new) is the
        idiomatic ib_insync modification and avoids leaving the position
        unprotected in the window between a cancel and a fresh placement.
        """
        if self._ib is None:
            return False
        bracket = self._brackets.get(symbol)
        if bracket is None:
            return False
        new_stop = round(new_stop, 2)
        if new_stop <= float(bracket["stop_price"]):
            return False
        stop_trade = bracket.get("stop_trade")
        order = getattr(stop_trade, "order", None)
        if order is None:
            return False
        try:
            order.auxPrice = new_stop  # STP orders carry the trigger in auxPrice
            self._ib.placeOrder(bracket["contract"], order)
        except Exception as exc:  # noqa: BLE001
            self._log.error("ibkr.modify_stop_failed", symbol=symbol, error=str(exc))
            return False
        bracket["stop_price"] = new_stop
        self._log.info("ibkr.stop_modified", symbol=symbol, new_stop=new_stop)
        return True

    # ------------------------------------------------------------- force close

    def force_close(self, symbol: str, reason: ExitReason) -> Optional[ExitEvent]:
        """Cancel the resting bracket legs and market-sell the position."""
        if self._ib is None:
            return None
        bracket = self._brackets.get(symbol)
        if bracket is None:
            return None
        # Cancel both resting legs first so the market order is the only exit.
        for leg_key in ("stop_trade", "target_trade"):
            trade = bracket.get(leg_key)
            if trade is not None:
                try:
                    self._ib.cancelOrder(trade.order)
                except Exception as exc:  # noqa: BLE001
                    self._log.warning("ibkr.cancel_leg_failed", error=str(exc))
        try:
            from ib_insync import MarketOrder  # type: ignore

            order = MarketOrder("SELL", int(bracket["quantity"]))
            trade = self._ib.placeOrder(bracket["contract"], order)
        except Exception as exc:  # noqa: BLE001
            self._log.error("ibkr.force_close_failed", symbol=symbol, error=str(exc))
            return None

        _, fill_price = self._leg_fill(trade)
        if not fill_price:
            # Market order not filled synchronously — fall back to last price.
            fill_price = fetch_current_price(symbol) or float(bracket["entry_price"])
        entry = float(bracket["entry_price"])
        qty = int(bracket["quantity"])
        del self._brackets[symbol]
        self._log.info(
            "ibkr.force_closed",
            symbol=symbol,
            reason=reason.value,
            exit_price=round(fill_price, 4),
        )
        return ExitEvent(
            symbol=symbol,
            exit_price=round(fill_price, 4),
            exit_reason=reason,
            exit_date=datetime.now(),
            pnl_gross=round((fill_price - entry) * qty, 2),
            fill_details={"quantity": qty},
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_broker(settings: Settings) -> Broker:
    """Return the broker selected by ``settings.BROKER`` (paper | ibkr)."""
    if settings.BROKER.lower() == "ibkr":
        return IBKRBroker(settings)  # type: ignore[return-value]
    return PaperBroker(settings)  # type: ignore[return-value]
