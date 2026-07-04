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


class Broker(Protocol):
    """Minimal broker interface the engine depends on."""

    def connect(self) -> bool: ...

    def disconnect(self) -> None: ...

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

    Bracket orders fill immediately at the requested entry price.  On each
    :meth:`poll_exits` call the broker fetches the latest daily bar for every
    open position and closes any position whose bar low pierced the stop
    (``STOP_HIT``) or whose bar high reached the target (``TARGET_HIT``).  When
    both are touched in the same bar, the stop is assumed to fill first (the
    conservative assumption).

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
        """Simulate a bracket order that fills at *entry_price* immediately."""
        if quantity <= 0:
            return BracketResult(False, reason="zero_quantity")
        if symbol in self._positions:
            return BracketResult(False, reason="already_open")

        self._order_seq += 1
        order_id = f"PAPER-{self._order_seq:06d}"
        self._positions[symbol] = _PaperPosition(
            symbol=symbol,
            quantity=quantity,
            entry_price=round(entry_price, 4),
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
            entry_price=entry_price,
            order_id=order_id,
        )
        return BracketResult(
            accepted=True,
            fill_price=round(entry_price, 4),
            order_id=order_id,
            commission=0.0,
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
            # Fall back to the current price as both high and low.
            price = fetch_current_price(pos.symbol)
            if price is None:
                return None
            bar_low = bar_high = price
        else:
            last = df.iloc[-1]
            bar_low = float(last["Low"])
            bar_high = float(last["High"])

        # Stop assumed to fill first when both are touched (conservative).
        if bar_low <= pos.stop_price:
            return self._build_exit(pos, pos.stop_price, ExitReason.STOP_HIT)
        if bar_high >= pos.target_price:
            return self._build_exit(pos, pos.target_price, ExitReason.TARGET_HIT)
        return None

    def _build_exit(
        self, pos: _PaperPosition, exit_price: float, reason: ExitReason
    ) -> ExitEvent:
        pnl = (exit_price - pos.entry_price) * pos.quantity
        self._log.info(
            "paper_broker.exit",
            symbol=pos.symbol,
            reason=reason.value,
            exit_price=exit_price,
            pnl_gross=round(pnl, 2),
        )
        return ExitEvent(
            symbol=pos.symbol,
            exit_price=round(exit_price, 4),
            exit_reason=reason,
            exit_date=datetime.now(),
            pnl_gross=round(pnl, 2),
            fill_details={"order_id": pos.order_id, "quantity": pos.quantity},
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

    This is a thin wrapper that connects to a running TWS/Gateway via
    ib_insync and places native bracket orders.  ib_insync is imported lazily
    inside :meth:`connect` so importing this module never requires the package.

    Only the subset the engine needs is implemented; richer server-side fill
    reconciliation (``reqExecutions`` across sessions) is left as a documented
    extension point.  For unattended server deployments prefer
    :class:`PaperBroker`.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._ib = None
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
            bracket = self._ib.bracketOrder(
                "BUY",
                quantity,
                limitPrice=round(entry_price * 1.002, 2),
                takeProfitPrice=round(target_price, 2),
                stopLossPrice=round(stop_price, 2),
            )
            for order in bracket:
                self._ib.placeOrder(contract, order)
            return BracketResult(
                accepted=True,
                fill_price=entry_price,
                order_id=str(bracket[0].orderId),
                reason="submitted",
            )
        except Exception as exc:  # noqa: BLE001
            self._log.error("ibkr.place_failed", symbol=symbol, error=str(exc))
            return BracketResult(False, reason=f"error:{exc}")

    def poll_exits(self) -> List[ExitEvent]:
        # A full implementation would reconcile reqExecutions() against a local
        # journal.  Not implemented for the optional live path.
        return []

    def modify_stop(self, symbol: str, new_stop: float) -> bool:
        return False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_broker(settings: Settings) -> Broker:
    """Return the broker selected by ``settings.BROKER`` (paper | ibkr)."""
    if settings.BROKER.lower() == "ibkr":
        return IBKRBroker(settings)  # type: ignore[return-value]
    return PaperBroker(settings)  # type: ignore[return-value]
