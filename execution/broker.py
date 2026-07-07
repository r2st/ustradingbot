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
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

import structlog

from config.settings import Settings
from data.fetcher import fetch_current_price, fetch_ohlcv
from execution.advanced_orders import (
    expiry_at,
    is_expired,
    partial_take_price,
    partial_take_split,
)
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


def short_entry_slippage(entry_price: float, slippage_bps: float) -> float:
    """Return the short-entry fill price after adverse slippage.

    A short sale fills *below* the requested price by ``slippage_bps``.
    """
    return round(entry_price * (1.0 - slippage_bps / 10_000.0), 4)


def short_stop_exit_fill(
    stop_price: float, bar_open: float, slippage_bps: float
) -> float:
    """Fill price for a short position's buy-stop that triggered on this bar.

    A gap *above* the stop fills at the (worse) open; otherwise the stop
    fills with adverse slippage upward.
    """
    if bar_open > stop_price:
        return round(bar_open, 4)
    return round(stop_price * (1.0 + slippage_bps / 10_000.0), 4)


def short_target_exit_fill(target_price: float, bar_open: float) -> float:
    """Fill price for a short position's buy-to-cover target.

    Fills at the target, or *better* (lower) if the bar gapped open below it.
    """
    if bar_open < target_price:
        return round(bar_open, 4)
    return round(target_price, 4)


def position_pnl(side: str, entry: float, exit_price: float, quantity: int) -> float:
    """Signed gross P&L for a lot, long or short."""
    if str(side).lower() == "short":
        return (entry - exit_price) * quantity
    return (exit_price - entry) * quantity


def _fmt_gtd(placed_at: datetime, expiry_hours: float) -> str:
    """Format an IBKR good-till-date string (``YYYYMMDD HH:MM:SS``)."""
    return expiry_at(placed_at, expiry_hours).strftime("%Y%m%d %H:%M:%S")


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
        partial_take_pct: float = 0.0,
        partial_take_target_r: float = 0.0,
    ) -> BracketResult: ...

    def place_manual_bracket(
        self,
        symbol: str,
        side: str,
        quantity: int,
        entry_price: float,
        levels: List[Dict[str, Any]],
        currency: str = "USD",
    ) -> BracketResult:
        """Open a manual position (long or short) with a multi-level exit ladder.

        *levels* is a list of serialised :class:`execution.levels.ExitLevel`
        dicts (``kind``/``price``/``quantity``).  Each triggered level exits
        its slice; the ladder as a whole always covers the full position.
        """
        ...

    def place_limit_order(
        self,
        symbol: str,
        quantity: int,
        limit_price: float,
        stop_price: float,
        target_price: float,
        currency: str = "USD",
        expiry_hours: float = 4.0,
        partial_take_pct: float = 0.0,
        partial_take_target_r: float = 0.0,
    ) -> BracketResult:
        """Place a resting limit entry that expires after *expiry_hours*."""
        ...

    def place_scale_in(
        self,
        symbol: str,
        tranches: List[tuple],
        stop_price: float,
        target_price: float,
        currency: str = "USD",
        expiry_hours: float = 4.0,
        partial_take_pct: float = 0.0,
        partial_take_target_r: float = 0.0,
    ) -> List[BracketResult]:
        """Place several resting limit tranches ``[(price, qty), ...]``."""
        ...

    def place_moc_order(
        self,
        symbol: str,
        quantity: int,
        stop_price: float,
        target_price: float,
        currency: str = "USD",
        partial_take_pct: float = 0.0,
        partial_take_target_r: float = 0.0,
    ) -> BracketResult:
        """Place a market-on-close entry that fills at the closing auction."""
        ...

    def poll_exits(self) -> List[ExitEvent]:
        """Detect and return any bracket legs (stop/target) that have filled."""

    def poll_pending_entries(self) -> List["PendingFill"]:
        """Fill or expire resting entry orders; return what happened."""
        ...

    def modify_stop(self, symbol: str, new_stop: float) -> bool: ...

    def force_close(self, symbol: str, reason: ExitReason) -> Optional[ExitEvent]:
        """Close *symbol* at market and return the resulting exit event."""
        ...


# ---------------------------------------------------------------------------
# Paper broker
# ---------------------------------------------------------------------------


@dataclass
class _PaperPosition:
    """Internal record for a simulated open position.

    ``partial_take_price``/``partial_take_pct`` configure one-shot partial
    profit-taking: when the bar high first reaches ``partial_take_price`` the
    broker sells ``partial_take_pct`` of the shares (emitting a ``PARTIAL_TAKE``
    event), sets ``partial_taken``, and lets the remainder run under the
    dynamic trailing stop.

    ``side`` is ``"long"`` (default) or ``"short"`` (manual sell trades);
    ``levels`` holds a multi-level exit ladder (list of serialised
    :class:`execution.levels.ExitLevel` dicts) for manual trades.  When
    ``levels`` is non-empty the ladder replaces the single stop/target pair
    as the exit logic (``stop_price``/``target_price`` then just mirror the
    nearest untriggered rung for display).
    """

    symbol: str
    quantity: int
    entry_price: float
    stop_price: float
    target_price: float
    currency: str
    order_id: str
    opened_at: str
    partial_take_price: float = 0.0
    partial_take_pct: float = 0.0
    partial_taken: bool = False
    side: str = "long"
    levels: List[Dict[str, Any]] = dc_field(default_factory=list)

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
            "partial_take_price": self.partial_take_price,
            "partial_take_pct": self.partial_take_pct,
            "partial_taken": self.partial_taken,
            "side": self.side,
            "levels": list(self.levels),
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
            partial_take_price=float(d.get("partial_take_price", 0.0) or 0.0),
            partial_take_pct=float(d.get("partial_take_pct", 0.0) or 0.0),
            partial_taken=bool(d.get("partial_taken", False)),
            side=str(d.get("side", "long") or "long"),
            levels=list(d.get("levels", []) or []),
        )


@dataclass
class _PendingOrder:
    """A resting simulated entry order (limit / scale-in tranche / MOC).

    Limit and scale-in orders fill when the market trades at or below
    ``limit_price``; a market-on-close order fills at the next poll (modelling
    the closing auction).  Any unfilled order is cancelled once it has been
    resting longer than ``expiry_hours``.
    """

    symbol: str
    quantity: int
    limit_price: float
    stop_price: float
    target_price: float
    currency: str
    order_id: str
    placed_at: str
    expiry_hours: float
    kind: str = "limit"  # "limit" | "scale_in" | "moc"
    partial_take_pct: float = 0.0
    partial_take_target_r: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "currency": self.currency,
            "order_id": self.order_id,
            "placed_at": self.placed_at,
            "expiry_hours": self.expiry_hours,
            "kind": self.kind,
            "partial_take_pct": self.partial_take_pct,
            "partial_take_target_r": self.partial_take_target_r,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "_PendingOrder":
        return cls(
            symbol=d["symbol"],
            quantity=int(d["quantity"]),
            limit_price=float(d["limit_price"]),
            stop_price=float(d["stop_price"]),
            target_price=float(d["target_price"]),
            currency=d.get("currency", "USD"),
            order_id=d.get("order_id", ""),
            placed_at=d.get("placed_at", datetime.now().isoformat()),
            expiry_hours=float(d.get("expiry_hours", 0.0) or 0.0),
            kind=d.get("kind", "limit"),
            partial_take_pct=float(d.get("partial_take_pct", 0.0) or 0.0),
            partial_take_target_r=float(d.get("partial_take_target_r", 0.0) or 0.0),
        )


@dataclass
class PendingFill:
    """Outcome of a resting entry order on a :meth:`poll_pending_entries` sweep.

    Exactly one of ``filled`` / ``expired`` is ``True``.  A fill carries the
    execution details the engine needs to journal the entry and register the
    position; an expiry simply reports the cancelled order.
    """

    symbol: str
    order_id: str
    filled: bool = False
    expired: bool = False
    fill_price: float = 0.0
    quantity: int = 0
    stop_price: float = 0.0
    target_price: float = 0.0
    currency: str = "USD"
    commission: float = 0.0
    kind: str = "limit"


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
        loaded = self._load()
        self._positions: Dict[str, _PaperPosition] = loaded[0]
        # Resting entry orders keyed by symbol (a list, since scale-in places
        # several tranches for one symbol).
        self._pending: Dict[str, List[_PendingOrder]] = loaded[1]
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
        partial_take_pct: float = 0.0,
        partial_take_target_r: float = 0.0,
    ) -> BracketResult:
        """Simulate a bracket order filling immediately with slippage + commission.

        The entry fills at *entry_price* plus ``PAPER_SLIPPAGE_BPS`` of adverse
        slippage, and a per-share commission is charged and returned on the
        :class:`BracketResult`.

        When *partial_take_pct* is in ``(0, 1)`` the position is armed for
        one-shot partial profit-taking at ``entry + target_r * risk`` (see
        :meth:`poll_exits`).
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
        pt_price = 0.0
        if 0.0 < partial_take_pct < 1.0:
            computed = partial_take_price(fill_price, stop_price, partial_take_target_r)
            pt_price = computed if computed is not None else 0.0
        self._positions[symbol] = _PaperPosition(
            symbol=symbol,
            quantity=quantity,
            entry_price=fill_price,
            stop_price=round(stop_price, 4),
            target_price=round(target_price, 4),
            currency=currency,
            order_id=order_id,
            opened_at=datetime.now().isoformat(),
            partial_take_price=pt_price,
            partial_take_pct=partial_take_pct if pt_price > 0 else 0.0,
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
            partial_take_price=pt_price,
        )
        return BracketResult(
            accepted=True,
            fill_price=fill_price,
            order_id=order_id,
            commission=commission,
            reason="filled",
        )

    def place_manual_bracket(
        self,
        symbol: str,
        side: str,
        quantity: int,
        entry_price: float,
        levels: List[Dict[str, Any]],
        currency: str = "USD",
    ) -> BracketResult:
        """Open a manual long or short position with a multi-level exit ladder.

        Fills immediately at *entry_price* plus adverse slippage (upward for a
        buy, downward for a short sale).  The ladder is stored on the position
        and evaluated by :meth:`poll_exits`.
        """
        from execution.levels import ExitLevel, nearest_price

        side = str(side).lower()
        if side not in ("long", "short"):
            return BracketResult(False, reason=f"invalid_side:{side}")
        if quantity <= 0:
            return BracketResult(False, reason="zero_quantity")
        if symbol in self._positions:
            return BracketResult(False, reason="already_open")
        if not levels:
            return BracketResult(False, reason="no_exit_levels")

        parsed = [ExitLevel.from_dict(l) for l in levels]
        if side == "long":
            fill_price = apply_entry_slippage(
                entry_price, self._settings.PAPER_SLIPPAGE_BPS
            )
        else:
            fill_price = short_entry_slippage(
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
            stop_price=nearest_price(parsed, "stop", side),
            target_price=nearest_price(parsed, "target", side),
            currency=currency,
            order_id=order_id,
            opened_at=datetime.now().isoformat(),
            side=side,
            levels=[l.to_dict() for l in parsed],
        )
        self._save()
        self._log.info(
            "paper_broker.manual_filled",
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=fill_price,
            levels=len(parsed),
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

    # ------------------------------------------------ resting entry orders

    def place_limit_order(
        self,
        symbol: str,
        quantity: int,
        limit_price: float,
        stop_price: float,
        target_price: float,
        currency: str = "USD",
        expiry_hours: float = 4.0,
        partial_take_pct: float = 0.0,
        partial_take_target_r: float = 0.0,
    ) -> BracketResult:
        """Rest a limit entry that fills on a dip to *limit_price* and expires."""
        if quantity <= 0:
            return BracketResult(False, reason="zero_quantity")
        self._order_seq += 1
        order_id = f"PAPER-{self._order_seq:06d}"
        self._pending.setdefault(symbol, []).append(
            _PendingOrder(
                symbol=symbol,
                quantity=int(quantity),
                limit_price=round(limit_price, 4),
                stop_price=round(stop_price, 4),
                target_price=round(target_price, 4),
                currency=currency,
                order_id=order_id,
                placed_at=datetime.now().isoformat(),
                expiry_hours=float(expiry_hours),
                kind="limit",
                partial_take_pct=partial_take_pct,
                partial_take_target_r=partial_take_target_r,
            )
        )
        self._save()
        self._log.info(
            "paper_broker.limit_placed",
            symbol=symbol,
            quantity=quantity,
            limit_price=limit_price,
            expiry_hours=expiry_hours,
            order_id=order_id,
        )
        return BracketResult(True, order_id=order_id, reason="resting")

    def place_scale_in(
        self,
        symbol: str,
        tranches: List[tuple],
        stop_price: float,
        target_price: float,
        currency: str = "USD",
        expiry_hours: float = 4.0,
        partial_take_pct: float = 0.0,
        partial_take_target_r: float = 0.0,
    ) -> List[BracketResult]:
        """Rest each ``(limit_price, qty)`` tranche as a separate limit order."""
        results: List[BracketResult] = []
        for price, qty in tranches:
            res = self.place_limit_order(
                symbol,
                int(qty),
                float(price),
                stop_price,
                target_price,
                currency=currency,
                expiry_hours=expiry_hours,
                partial_take_pct=partial_take_pct,
                partial_take_target_r=partial_take_target_r,
            )
            # Tag as scale-in so persistence/inspection can distinguish it.
            if res.accepted and symbol in self._pending:
                self._pending[symbol][-1].kind = "scale_in"
            results.append(res)
        self._save()
        return results

    def place_moc_order(
        self,
        symbol: str,
        quantity: int,
        stop_price: float,
        target_price: float,
        currency: str = "USD",
        partial_take_pct: float = 0.0,
        partial_take_target_r: float = 0.0,
    ) -> BracketResult:
        """Rest a market-on-close entry (fills at the next pending sweep)."""
        if quantity <= 0:
            return BracketResult(False, reason="zero_quantity")
        self._order_seq += 1
        order_id = f"PAPER-{self._order_seq:06d}"
        self._pending.setdefault(symbol, []).append(
            _PendingOrder(
                symbol=symbol,
                quantity=int(quantity),
                limit_price=0.0,  # MOC fills at market on the auction
                stop_price=round(stop_price, 4),
                target_price=round(target_price, 4),
                currency=currency,
                order_id=order_id,
                placed_at=datetime.now().isoformat(),
                expiry_hours=0.0,  # MOC never times out; it fills at the close
                kind="moc",
                partial_take_pct=partial_take_pct,
                partial_take_target_r=partial_take_target_r,
            )
        )
        self._save()
        self._log.info(
            "paper_broker.moc_placed", symbol=symbol, quantity=quantity, order_id=order_id
        )
        return BracketResult(True, order_id=order_id, reason="resting_moc")

    def poll_pending_entries(self) -> List["PendingFill"]:
        """Fill eligible resting entries and expire stale ones.

        A limit / scale-in order fills when the latest price is at or below its
        limit; an MOC order fills at the latest price.  Filled orders open (or
        average into) a position; expired orders are dropped.
        """
        fills: List[PendingFill] = []
        now = datetime.now()
        changed = False
        for symbol in list(self._pending.keys()):
            remaining: List[_PendingOrder] = []
            for order in self._pending[symbol]:
                if is_expired(order.placed_at, order.expiry_hours, now):
                    changed = True
                    fills.append(
                        PendingFill(
                            symbol=symbol,
                            order_id=order.order_id,
                            expired=True,
                            quantity=order.quantity,
                            currency=order.currency,
                            kind=order.kind,
                        )
                    )
                    self._log.info(
                        "paper_broker.entry_expired",
                        symbol=symbol,
                        order_id=order.order_id,
                    )
                    continue

                price = fetch_current_price(symbol)
                if price is None:
                    remaining.append(order)
                    continue

                fillable = order.kind == "moc" or price <= order.limit_price
                if not fillable:
                    remaining.append(order)
                    continue

                fill = self._fill_pending(order, price)
                fills.append(fill)
                changed = True

            if remaining:
                self._pending[symbol] = remaining
            else:
                del self._pending[symbol]

        if changed:
            self._save()
        return fills

    def _fill_pending(self, order: _PendingOrder, price: float) -> "PendingFill":
        """Fill one resting order at *price*, opening or averaging a position."""
        fill_price = apply_entry_slippage(price, self._settings.PAPER_SLIPPAGE_BPS)
        commission = commission_for(
            order.quantity, self._settings.PAPER_COMMISSION_PER_SHARE
        )
        existing = self._positions.get(order.symbol)
        if existing is None:
            pt_price = 0.0
            if 0.0 < order.partial_take_pct < 1.0:
                computed = partial_take_price(
                    fill_price, order.stop_price, order.partial_take_target_r
                )
                pt_price = computed if computed is not None else 0.0
            self._positions[order.symbol] = _PaperPosition(
                symbol=order.symbol,
                quantity=order.quantity,
                entry_price=fill_price,
                stop_price=order.stop_price,
                target_price=order.target_price,
                currency=order.currency,
                order_id=order.order_id,
                opened_at=datetime.now().isoformat(),
                partial_take_price=pt_price,
                partial_take_pct=order.partial_take_pct if pt_price > 0 else 0.0,
            )
        else:
            # Average the new tranche into the existing position.
            total_qty = existing.quantity + order.quantity
            existing.entry_price = round(
                (existing.entry_price * existing.quantity + fill_price * order.quantity)
                / total_qty,
                4,
            )
            existing.quantity = total_qty
        self._log.info(
            "paper_broker.entry_filled",
            symbol=order.symbol,
            kind=order.kind,
            fill_price=fill_price,
            quantity=order.quantity,
            order_id=order.order_id,
        )
        return PendingFill(
            symbol=order.symbol,
            order_id=order.order_id,
            filled=True,
            fill_price=fill_price,
            quantity=order.quantity,
            stop_price=order.stop_price,
            target_price=order.target_price,
            currency=order.currency,
            commission=commission,
            kind=order.kind,
        )

    def get_pending_orders(self) -> Dict[str, List[Dict[str, Any]]]:
        """Return a serialisable view of resting entry orders (for inspection)."""
        return {s: [o.to_dict() for o in orders] for s, orders in self._pending.items()}

    # ---------------------------------------------------------- poll exits

    def poll_exits(self) -> List[ExitEvent]:
        """Close positions on stop/target and take partial profit when armed.

        Returns one event per symbol per sweep.  A ``PARTIAL_TAKE`` event does
        *not* close the position — it trims the share count and lets the
        remainder run — so the symbol survives into later sweeps.
        """
        events: List[ExitEvent] = []
        for symbol in list(self._positions.keys()):
            pos = self._positions[symbol]
            if pos.levels:
                level_events, closed = self._check_level_exits(pos)
                events.extend(level_events)
                if closed:
                    del self._positions[symbol]
                continue
            result = self._check_position_exit(pos)
            if result is None:
                continue
            event, closed = result
            events.append(event)
            if closed:
                del self._positions[symbol]
        if events:
            self._save()
        return events

    def _latest_bar(self, symbol: str) -> Optional[tuple[float, float, float]]:
        """Return today's ``(open, low, high)``, falling back to last price."""
        df = fetch_ohlcv(symbol, period="5d")
        if df is None or df.empty:
            price = fetch_current_price(symbol)
            if price is None:
                return None
            return price, price, price
        last = df.iloc[-1]
        return float(last["Open"]), float(last["Low"]), float(last["High"])

    def _check_level_exits(
        self, pos: _PaperPosition
    ) -> tuple[List[ExitEvent], bool]:
        """Evaluate a multi-level exit ladder against the latest bar.

        Stop levels are checked before target levels (the conservative
        assumption when both sides are touched in one bar).  Several levels
        can trigger in the same sweep — a gap can blow through more than one
        stop — and each triggered level emits its own event: ``PARTIAL_TAKE``
        while shares remain, ``STOP_HIT``/``TARGET_HIT`` for the rung that
        empties the position.
        """
        from execution.levels import ExitLevel, nearest_price

        bar = self._latest_bar(pos.symbol)
        if bar is None:
            return [], False
        bar_open, bar_low, bar_high = bar
        slip = self._settings.PAPER_SLIPPAGE_BPS
        is_long = pos.side != "short"

        levels = [ExitLevel.from_dict(l) for l in pos.levels]
        events: List[ExitEvent] = []

        for kind in ("stop", "target"):
            for lvl in levels:
                if pos.quantity <= 0:
                    break
                if lvl.triggered or lvl.kind != kind or lvl.quantity <= 0:
                    continue
                if kind == "stop":
                    hit = bar_low <= lvl.price if is_long else bar_high >= lvl.price
                    if not hit:
                        continue
                    fill = (
                        stop_exit_fill(lvl.price, bar_open, slip)
                        if is_long
                        else short_stop_exit_fill(lvl.price, bar_open, slip)
                    )
                    close_reason = ExitReason.STOP_HIT
                else:
                    hit = bar_high >= lvl.price if is_long else bar_low <= lvl.price
                    if not hit:
                        continue
                    fill = (
                        target_exit_fill(lvl.price, bar_open)
                        if is_long
                        else short_target_exit_fill(lvl.price, bar_open)
                    )
                    close_reason = ExitReason.TARGET_HIT

                lvl.triggered = True
                exit_qty = min(lvl.quantity, pos.quantity)
                pos.quantity -= exit_qty
                reason = close_reason if pos.quantity == 0 else ExitReason.PARTIAL_TAKE
                event = self._build_exit(pos, fill, reason, exit_qty)
                event.fill_details["level"] = f"{kind}@{lvl.price}"
                events.append(event)

        pos.levels = [l.to_dict() for l in levels]
        # Mirror the nearest untriggered rungs into the legacy display fields.
        pos.stop_price = nearest_price(levels, "stop", pos.side) or pos.stop_price
        pos.target_price = nearest_price(levels, "target", pos.side) or pos.target_price
        return events, pos.quantity <= 0

    def _check_position_exit(
        self, pos: _PaperPosition
    ) -> Optional[tuple[ExitEvent, bool]]:
        """Return ``(event, closed)`` if *pos* transitioned, else ``None``.

        Priority order (at most one transition per sweep): full stop (closes),
        then partial-take (does not close), then full target (closes).
        """
        bar = self._latest_bar(pos.symbol)
        if bar is None:
            return None
        bar_open, bar_low, bar_high = bar

        slippage_bps = self._settings.PAPER_SLIPPAGE_BPS

        # Stop assumed to fill first when both are touched (conservative).
        # A gap-through open fills at the (worse) open price, not the stop.
        if bar_low <= pos.stop_price:
            fill = stop_exit_fill(pos.stop_price, bar_open, slippage_bps)
            return self._build_exit(pos, fill, ExitReason.STOP_HIT, pos.quantity), True

        # Partial profit-taking: sell a slice at the first target, keep runner.
        if (
            not pos.partial_taken
            and pos.partial_take_price > 0
            and pos.partial_take_pct > 0
            and bar_high >= pos.partial_take_price
        ):
            take_qty, runner_qty = partial_take_split(
                pos.quantity, pos.partial_take_pct
            )
            if take_qty > 0 and runner_qty > 0:
                fill = target_exit_fill(pos.partial_take_price, bar_open)
                event = self._build_exit(
                    pos, fill, ExitReason.PARTIAL_TAKE, take_qty
                )
                pos.quantity = runner_qty
                pos.partial_taken = True
                return event, False

        if bar_high >= pos.target_price:
            fill = target_exit_fill(pos.target_price, bar_open)
            return self._build_exit(pos, fill, ExitReason.TARGET_HIT, pos.quantity), True
        return None

    def _build_exit(
        self,
        pos: _PaperPosition,
        exit_price: float,
        reason: ExitReason,
        quantity: Optional[int] = None,
    ) -> ExitEvent:
        qty = pos.quantity if quantity is None else int(quantity)
        pnl = position_pnl(pos.side, pos.entry_price, exit_price, qty)
        commission = commission_for(
            qty, self._settings.PAPER_COMMISSION_PER_SHARE
        )
        self._log.info(
            "paper_broker.exit",
            symbol=pos.symbol,
            side=pos.side,
            reason=reason.value,
            exit_price=exit_price,
            quantity=qty,
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
                "quantity": qty,
                "commission": commission,
                "partial": reason == ExitReason.PARTIAL_TAKE,
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
        # Laddered manual positions own their exit levels; the single-stop
        # ratchet does not apply (future dynamic stops will re-price the
        # ladder itself via execution.levels.reprice_stop_levels).
        if pos.levels or pos.side == "short":
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
        order_ids: List[str] = [p.order_id for p in self._positions.values()]
        for pending_list in self._pending.values():
            order_ids.extend(o.order_id for o in pending_list)
        for oid in order_ids:
            try:
                seq = max(seq, int(oid.split("-")[-1]))
            except (ValueError, IndexError):
                continue
        return seq

    def _load(
        self,
    ) -> tuple[Dict[str, _PaperPosition], Dict[str, List[_PendingOrder]]]:
        if not self._path.exists():
            return {}, {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            positions = {
                s: _PaperPosition.from_dict(d)
                for s, d in raw.get("positions", {}).items()
            }
            pending = {
                s: [_PendingOrder.from_dict(o) for o in orders]
                for s, orders in raw.get("pending", {}).items()
            }
            return positions, pending
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            return {}, {}

    def _save(self) -> None:
        payload = {
            "positions": {s: p.to_dict() for s, p in self._positions.items()},
            "pending": {
                s: [o.to_dict() for o in orders]
                for s, orders in self._pending.items()
            },
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
        # symbol -> list of resting entry records (limit / scale-in / MOC)
        self._pending_entries: Dict[str, List[Dict[str, Any]]] = {}
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
        partial_take_pct: float = 0.0,
        partial_take_target_r: float = 0.0,
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
                "partial_take_pct": 0.0,
                "partial_taken": False,
                "partial_trade": None,
            }
            self._arm_partial_take(
                symbol, entry_price, stop_price, partial_take_pct, partial_take_target_r
            )
            return BracketResult(
                accepted=True,
                fill_price=entry_price,
                order_id=str(bracket[0].orderId),
                reason="submitted",
            )
        except Exception as exc:  # noqa: BLE001
            self._log.error("ibkr.place_failed", symbol=symbol, error=str(exc))
            return BracketResult(False, reason=f"error:{exc}")

    def place_manual_bracket(
        self,
        symbol: str,
        side: str,
        quantity: int,
        entry_price: float,
        levels: List[Dict[str, Any]],
        currency: str = "USD",
    ) -> BracketResult:
        """Multi-level / short manual orders are not implemented for IBKR yet.

        Single-level long manual trades route through
        :meth:`place_bracket_order` (native bracket) instead — see
        ``execution.manual_trade.place_manual_trade``.
        """
        return BracketResult(
            False,
            reason=(
                "multi-level / short manual orders are not supported on the "
                "IBKR broker yet — use a single stop + target long trade, or "
                "the paper broker"
            ),
        )

    def _arm_partial_take(
        self,
        symbol: str,
        entry_price: float,
        stop_price: float,
        partial_take_pct: float,
        partial_take_target_r: float,
    ) -> None:
        """Place an extra take-profit leg for the partial-take slice, if armed."""
        bracket = self._brackets.get(symbol)
        if bracket is None or not (0.0 < partial_take_pct < 1.0):
            return
        pt_price = partial_take_price(entry_price, stop_price, partial_take_target_r)
        if pt_price is None:
            return
        take_qty, _ = partial_take_split(bracket["quantity"], partial_take_pct)
        if take_qty <= 0:
            return
        try:
            from ib_insync import LimitOrder  # type: ignore

            order = LimitOrder("SELL", take_qty, round(pt_price, 2))
            trade = self._ib.placeOrder(bracket["contract"], order)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("ibkr.partial_arm_failed", symbol=symbol, error=str(exc))
            return
        bracket["partial_take_pct"] = partial_take_pct
        bracket["partial_take_price"] = round(pt_price, 2)
        bracket["partial_take_qty"] = take_qty
        bracket["partial_trade"] = trade

    # ------------------------------------------------ resting entry orders

    def place_limit_order(
        self,
        symbol: str,
        quantity: int,
        limit_price: float,
        stop_price: float,
        target_price: float,
        currency: str = "USD",
        expiry_hours: float = 4.0,
        partial_take_pct: float = 0.0,
        partial_take_target_r: float = 0.0,
    ) -> BracketResult:
        """Submit a resting GTD limit entry; the bracket is attached on fill."""
        if self._ib is None:
            return BracketResult(False, reason="not_connected")
        try:
            from ib_insync import LimitOrder, Stock  # type: ignore

            contract = Stock(symbol, "SMART", currency)
            self._ib.qualifyContracts(contract)
            order = LimitOrder("BUY", int(quantity), round(limit_price, 2))
            # Good-till-date time-in-force enforces the expiry broker-side.
            order.tif = "GTD"
            order.goodTillDate = _fmt_gtd(datetime.now(), expiry_hours)
            trade = self._ib.placeOrder(contract, order)
        except Exception as exc:  # noqa: BLE001
            self._log.error("ibkr.limit_failed", symbol=symbol, error=str(exc))
            return BracketResult(False, reason=f"error:{exc}")
        order_id = str(getattr(order, "orderId", ""))
        self._pending_entries.setdefault(symbol, []).append(
            {
                "contract": contract,
                "entry_trade": trade,
                "quantity": int(quantity),
                "limit_price": round(limit_price, 2),
                "stop_price": round(stop_price, 2),
                "target_price": round(target_price, 2),
                "currency": currency,
                "order_id": order_id,
                "placed_at": datetime.now().isoformat(),
                "expiry_hours": float(expiry_hours),
                "kind": "limit",
                "partial_take_pct": partial_take_pct,
                "partial_take_target_r": partial_take_target_r,
            }
        )
        return BracketResult(True, order_id=order_id, reason="resting")

    def place_scale_in(
        self,
        symbol: str,
        tranches: List[tuple],
        stop_price: float,
        target_price: float,
        currency: str = "USD",
        expiry_hours: float = 4.0,
        partial_take_pct: float = 0.0,
        partial_take_target_r: float = 0.0,
    ) -> List[BracketResult]:
        results: List[BracketResult] = []
        for price, qty in tranches:
            res = self.place_limit_order(
                symbol,
                int(qty),
                float(price),
                stop_price,
                target_price,
                currency=currency,
                expiry_hours=expiry_hours,
                partial_take_pct=partial_take_pct,
                partial_take_target_r=partial_take_target_r,
            )
            if res.accepted and symbol in self._pending_entries:
                self._pending_entries[symbol][-1]["kind"] = "scale_in"
            results.append(res)
        return results

    def place_moc_order(
        self,
        symbol: str,
        quantity: int,
        stop_price: float,
        target_price: float,
        currency: str = "USD",
        partial_take_pct: float = 0.0,
        partial_take_target_r: float = 0.0,
    ) -> BracketResult:
        """Submit a market-on-close entry; the bracket is attached on fill."""
        if self._ib is None:
            return BracketResult(False, reason="not_connected")
        try:
            from ib_insync import Order, Stock  # type: ignore

            contract = Stock(symbol, "SMART", currency)
            self._ib.qualifyContracts(contract)
            order = Order(action="BUY", totalQuantity=int(quantity), orderType="MOC")
            trade = self._ib.placeOrder(contract, order)
        except Exception as exc:  # noqa: BLE001
            self._log.error("ibkr.moc_failed", symbol=symbol, error=str(exc))
            return BracketResult(False, reason=f"error:{exc}")
        order_id = str(getattr(order, "orderId", ""))
        self._pending_entries.setdefault(symbol, []).append(
            {
                "contract": contract,
                "entry_trade": trade,
                "quantity": int(quantity),
                "limit_price": 0.0,
                "stop_price": round(stop_price, 2),
                "target_price": round(target_price, 2),
                "currency": currency,
                "order_id": order_id,
                "placed_at": datetime.now().isoformat(),
                "expiry_hours": 0.0,
                "kind": "moc",
                "partial_take_pct": partial_take_pct,
                "partial_take_target_r": partial_take_target_r,
            }
        )
        return BracketResult(True, order_id=order_id, reason="resting_moc")

    def poll_pending_entries(self) -> List["PendingFill"]:
        """Attach a bracket to each filled resting entry; expire stale ones."""
        if self._ib is None:
            return []
        fills: List[PendingFill] = []
        for symbol in list(self._pending_entries.keys()):
            remaining: List[Dict[str, Any]] = []
            for entry in self._pending_entries[symbol]:
                filled, fill_price = self._leg_fill(entry.get("entry_trade"))
                if filled:
                    fill_price = fill_price or float(entry["limit_price"])
                    self._attach_bracket_on_fill(symbol, entry, fill_price)
                    fills.append(
                        PendingFill(
                            symbol=symbol,
                            order_id=entry["order_id"],
                            filled=True,
                            fill_price=round(fill_price, 4),
                            quantity=entry["quantity"],
                            stop_price=entry["stop_price"],
                            target_price=entry["target_price"],
                            currency=entry["currency"],
                            kind=entry["kind"],
                        )
                    )
                    continue
                if is_expired(entry["placed_at"], entry["expiry_hours"]):
                    self._cancel_entry(entry)
                    fills.append(
                        PendingFill(
                            symbol=symbol,
                            order_id=entry["order_id"],
                            expired=True,
                            quantity=entry["quantity"],
                            currency=entry["currency"],
                            kind=entry["kind"],
                        )
                    )
                    continue
                remaining.append(entry)
            if remaining:
                self._pending_entries[symbol] = remaining
            else:
                del self._pending_entries[symbol]
        return fills

    def _attach_bracket_on_fill(
        self, symbol: str, entry: Dict[str, Any], fill_price: float
    ) -> None:
        """Place stop + target legs once a resting entry fills."""
        existing = self._brackets.get(symbol)
        if existing is not None:
            # Average a scale-in tranche into the existing tracked position.
            total = existing["quantity"] + entry["quantity"]
            existing["entry_price"] = round(
                (existing["entry_price"] * existing["quantity"]
                 + fill_price * entry["quantity"]) / total,
                4,
            )
            existing["quantity"] = total
            return
        try:
            from ib_insync import LimitOrder, StopOrder  # type: ignore

            qty = entry["quantity"]
            target = self._ib.placeOrder(
                entry["contract"], LimitOrder("SELL", qty, entry["target_price"])
            )
            stop = self._ib.placeOrder(
                entry["contract"], StopOrder("SELL", qty, entry["stop_price"])
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("ibkr.attach_bracket_failed", symbol=symbol, error=str(exc))
            target = stop = None
        self._brackets[symbol] = {
            "contract": entry["contract"],
            "quantity": entry["quantity"],
            "entry_price": round(fill_price, 4),
            "stop_price": entry["stop_price"],
            "target_price": entry["target_price"],
            "parent_trade": entry.get("entry_trade"),
            "target_trade": target,
            "stop_trade": stop,
            "partial_take_pct": 0.0,
            "partial_taken": False,
            "partial_trade": None,
        }
        self._arm_partial_take(
            symbol,
            fill_price,
            entry["stop_price"],
            entry.get("partial_take_pct", 0.0),
            entry.get("partial_take_target_r", 0.0),
        )

    def _cancel_entry(self, entry: Dict[str, Any]) -> None:
        trade = entry.get("entry_trade")
        order = getattr(trade, "order", None)
        if order is not None and self._ib is not None:
            try:
                self._ib.cancelOrder(order)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("ibkr.cancel_entry_failed", error=str(exc))

    # ------------------------------------------------------------- poll exits

    def poll_exits(self) -> List[ExitEvent]:
        """Detect bracket legs (partial / stop / target) that filled.

        Overrides the base sweep to also handle a partial-take leg: when it
        fills the position is trimmed (a ``PARTIAL_TAKE`` event is emitted) but
        the bracket keeps running for the remainder.
        """
        if self._ib is None:
            return []
        events: List[ExitEvent] = []
        for symbol in list(self._brackets.keys()):
            bracket = self._brackets[symbol]
            partial = self._detect_partial_fill(symbol, bracket)
            if partial is not None:
                events.append(partial)
                continue
            event = self._detect_bracket_fill(symbol, bracket)
            if event is not None:
                events.append(event)
                del self._brackets[symbol]
        return events

    def _detect_partial_fill(
        self, symbol: str, bracket: Dict[str, Any]
    ) -> Optional[ExitEvent]:
        """Emit a ``PARTIAL_TAKE`` event if the partial leg just filled."""
        if bracket.get("partial_taken") or bracket.get("partial_trade") is None:
            return None
        filled, fill_price = self._leg_fill(bracket.get("partial_trade"))
        if not filled:
            return None
        take_qty = int(bracket.get("partial_take_qty", 0))
        if take_qty <= 0:
            return None
        exit_price = fill_price or float(bracket.get("partial_take_price", 0.0))
        entry = float(bracket["entry_price"])
        bracket["partial_taken"] = True
        bracket["quantity"] = max(0, int(bracket["quantity"]) - take_qty)
        self._log.info(
            "ibkr.partial_take", symbol=symbol, exit_price=exit_price, quantity=take_qty
        )
        return ExitEvent(
            symbol=symbol,
            exit_price=round(exit_price, 4),
            exit_reason=ExitReason.PARTIAL_TAKE,
            exit_date=datetime.now(),
            pnl_gross=round((exit_price - entry) * take_qty, 2),
            fill_details={"quantity": take_qty, "partial": True},
        )

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
