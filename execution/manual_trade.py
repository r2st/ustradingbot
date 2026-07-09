"""
Manual trade entry (feature 2).

Lets an operator place a trade by hand from the dashboard — supplying the
symbol, side (buy/sell), entry, exit levels and quantity directly — bypassing
the screener / grading pipeline but reusing the *exact* same execution path as
an automated entry: a broker order, a journal entry row, and a registered
position.

Exits support **multi-level ladders**: several stop-loss levels and several
profit-target levels, each exiting a slice of the position (e.g. stop 1 at
-3% for 33%, stop 2 at -5% for 33%, stop 3 at -8% for the rest — and targets
mirrored on the upside).  A single ``stop_price``/``target_price`` pair is
still accepted (a one-rung ladder), and that shape routes through the classic
bracket order so it works identically on the
:class:`~execution.broker.PaperBroker` and the
:class:`~execution.broker.IBKRBroker`.  Multi-level ladders and short (sell)
entries run on the paper broker's level engine.

Because the dashboard runs in a separate process from the engine, the default
wiring builds a fresh broker / logger / risk-manager pointed at the same
``DATA_DIR``; all three persist to shared files, so the trade is immediately
visible on the dashboard and reconciled by the engine on its next cycle.

The heavy lifting is in :func:`place_manual_trade`, which is fully injectable
(broker / logger / risk-manager) so it unit-tests without a real broker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

import structlog

from config.universe import get_currency
from config.watchlist import WatchlistError, normalize_symbol
from execution.levels import (
    ExitLevel,
    LevelError,
    build_levels,
    nearest_price,
    normalize_side,
)
from signals.signal_types import Grade, Signal, TradeOrder

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class ManualTradeError(ValueError):
    """Raised when manual-trade parameters fail validation."""


@dataclass
class ManualTradeResult:
    """Outcome of a manual-trade placement."""

    ok: bool
    message: str
    symbol: str = ""
    quantity: int = 0
    fill_price: float = 0.0
    order_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "symbol": self.symbol,
            "quantity": self.quantity,
            "fill_price": round(self.fill_price, 4),
            "order_id": self.order_id,
        }


def _num(params: Dict[str, Any], key: str) -> float:
    raw = params.get(key, None)
    if raw is None or raw == "":
        raise ManualTradeError(f"{key} is required.")
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ManualTradeError(f"{key} must be a number.") from None


def validate_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and coerce raw manual-trade parameters.

    Required keys: ``symbol``, ``entry_price``, ``quantity``, and exit levels
    given either as ladders (``stops`` / ``targets`` — lists of
    ``{"price": ...}`` or ``{"percent": ...}`` items, each with an optional
    ``pct`` slice of the position) or as the legacy single ``stop_price`` /
    ``target_price`` pair.  ``side`` is ``"buy"`` (default, long) or
    ``"sell"`` (short); every stop must sit on the losing side of entry and
    every target on the winning side for the chosen direction.

    Returns a dict with ``symbol``, ``side``, ``entry_price``, ``quantity``,
    ``strategy``, resolved ``levels`` (:class:`ExitLevel` list), and the
    nearest ``stop_price`` / ``target_price``.

    Raises:
        ManualTradeError: on any invalid / missing field.
    """
    try:
        symbol = normalize_symbol(params.get("symbol", ""))
    except WatchlistError as exc:
        raise ManualTradeError(str(exc)) from exc

    try:
        side = normalize_side(params.get("side", "buy"))
    except LevelError as exc:
        raise ManualTradeError(str(exc)) from exc

    entry = _num(params, "entry_price")
    try:
        quantity = int(float(params.get("quantity")))
    except (TypeError, ValueError):
        raise ManualTradeError("quantity must be an integer.") from None

    if quantity <= 0:
        raise ManualTradeError("quantity must be a positive integer.")
    if entry <= 0:
        raise ManualTradeError("prices must be positive.")

    # Exit levels: ladders take precedence; otherwise fall back to the legacy
    # single stop/target pair (a one-rung ladder on each side).
    stop_specs = params.get("stops", None)
    target_specs = params.get("targets", None)
    if not stop_specs:
        stop = _num(params, "stop_price")
        if stop <= 0:
            raise ManualTradeError("prices must be positive.")
        if side == "long" and stop >= entry:
            raise ManualTradeError("stop_price must be below entry_price for a buy.")
        stop_specs = [{"price": stop, "pct": 100.0}]
    if not target_specs:
        target = _num(params, "target_price")
        if target <= 0:
            raise ManualTradeError("prices must be positive.")
        if side == "long" and target <= entry:
            raise ManualTradeError("target_price must be above entry_price for a buy.")
        target_specs = [{"price": target, "pct": 100.0}]

    try:
        levels = build_levels(side, entry, quantity, stop_specs, target_specs)
    except LevelError as exc:
        raise ManualTradeError(str(exc)) from exc

    strategy = str(params.get("strategy", "manual") or "manual").strip().lower()
    return {
        "symbol": symbol,
        "side": side,
        "entry_price": entry,
        "stop_price": nearest_price(levels, "stop", side),
        "target_price": nearest_price(levels, "target", side),
        "quantity": quantity,
        "strategy": strategy,
        "levels": levels,
    }


def _ladder_risk(levels: List[ExitLevel], entry: float, side: str) -> float:
    """Worst-case dollar risk: every stop rung filling at its price."""
    risk = 0.0
    for lvl in levels:
        if lvl.kind != "stop":
            continue
        per_share = (entry - lvl.price) if side == "long" else (lvl.price - entry)
        risk += max(0.0, per_share) * lvl.quantity
    return round(risk, 2)


def build_manual_order(params: Dict[str, Any]) -> TradeOrder:
    """Build a :class:`TradeOrder` from validated manual-trade *params*."""
    clean = validate_params(params)
    levels: List[ExitLevel] = clean["levels"]
    signal = Signal(
        symbol=clean["symbol"],
        strategy=clean["strategy"],
        direction=clean["side"],
        entry_price=clean["entry_price"],
        stop_price=clean["stop_price"],
        target_price=clean["target_price"],
        signal_strength=1.0,
        grade=Grade.A,  # manual entries bypass grading; recorded as top grade
        raw_data={
            "manual": True,
            "side": clean["side"],
            "levels": [l.to_dict() for l in levels],
        },
    )
    risk_amount = _ladder_risk(levels, clean["entry_price"], clean["side"])
    return TradeOrder(
        signal=signal,
        quantity=clean["quantity"],
        risk_amount=risk_amount,
        max_risk_dollars=risk_amount,
        currency=get_currency(clean["symbol"]),
        ai_decision="MANUAL",
        ai_reasoning="Manually entered from the dashboard.",
        ai_cost_usd=0.0,
    )


def _is_simple_long_bracket(order: TradeOrder) -> bool:
    """One stop + one target on a long — the classic bracket shape."""
    levels = order.signal.raw_data.get("levels", [])
    kinds = [l.get("kind") for l in levels]
    return (
        order.signal.direction == "long"
        and kinds.count("stop") == 1
        and kinds.count("target") == 1
    )


def place_manual_trade(
    params: Dict[str, Any],
    settings,
    broker: Any = None,
    trade_logger: Any = None,
    risk_manager: Any = None,
) -> ManualTradeResult:
    """Validate *params* and place a manual order (long/short, multi-level).

    A simple long with one stop and one target is placed as a classic bracket
    order (supported by every broker); anything else — several stop or target
    levels, or a short — goes through the broker's manual level engine
    (:meth:`~execution.broker.PaperBroker.place_manual_bracket`).

    Args:
        params: Raw form parameters (see :func:`validate_params`).
        settings: Application settings (used to build the default wiring).
        broker / trade_logger / risk_manager: Injected collaborators; when
            ``None`` a fresh instance pointed at ``settings.DATA_DIR`` is built.

    Returns:
        A :class:`ManualTradeResult`.  Validation failures return ``ok=False``
        with a message rather than raising.
    """
    try:
        order = build_manual_order(params)
    except ManualTradeError as exc:
        return ManualTradeResult(False, str(exc))

    # Build the default wiring lazily so tests can inject fakes without touching
    # a real broker or the filesystem.
    if broker is None:
        from execution.broker import make_broker

        broker = make_broker(settings)
    if trade_logger is None:
        from journal.trade_logger import TradeLogger

        trade_logger = TradeLogger(str(settings.DATA_DIR), trading_mode=settings.TRADING_MODE)
    if risk_manager is None:
        from risk.manager import RiskManager

        risk_manager = RiskManager(settings)

    sig = order.signal

    # Run risk management pre-checks (position limits, daily loss, cooldowns,
    # R:R) so manual trades cannot bypass safety guardrails.
    passed, reason = risk_manager.pre_check(sig)
    if not passed:
        return ManualTradeResult(False, f"Risk check failed: {reason}", sig.symbol)

    try:
        if not broker.is_connected():
            broker.connect()
    except Exception as exc:  # noqa: BLE001
        return ManualTradeResult(False, f"Broker connect failed: {exc}", sig.symbol)

    try:
        if _is_simple_long_bracket(order):
            result = broker.place_bracket_order(
                symbol=sig.symbol,
                quantity=order.quantity,
                entry_price=sig.entry_price,
                stop_price=sig.stop_price,
                target_price=sig.target_price,
                currency=order.currency,
            )
        else:
            result = broker.place_manual_bracket(
                symbol=sig.symbol,
                side=sig.direction,
                quantity=order.quantity,
                entry_price=sig.entry_price,
                levels=sig.raw_data.get("levels", []),
                currency=order.currency,
            )
    except AttributeError:
        return ManualTradeResult(
            False,
            "This broker does not support multi-level / sell manual orders.",
            sig.symbol,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("manual_trade.broker_error", symbol=sig.symbol, error=str(exc))
        return ManualTradeResult(False, f"Broker error: {exc}", sig.symbol)

    if not getattr(result, "accepted", False):
        return ManualTradeResult(
            False, f"Broker rejected the order: {getattr(result, 'reason', '')}",
            sig.symbol,
        )

    fill_price = float(getattr(result, "fill_price", 0.0) or 0.0)
    commission = float(getattr(result, "commission", 0.0) or 0.0)
    order_id = str(getattr(result, "order_id", "") or "")

    # Journal + register exactly like the automated path.
    try:
        trade_logger.log_entry(order, fill_price, commission)
        risk_manager.register_position(order, fill_price)
    except Exception as exc:  # noqa: BLE001 -- the fill happened; log but report success
        log.error("manual_trade.post_fill_error", symbol=sig.symbol, error=str(exc))

    action = "Bought" if sig.direction == "long" else "Sold short"
    log.info(
        "manual_trade.placed",
        symbol=sig.symbol, side=sig.direction, quantity=order.quantity,
        fill_price=fill_price, levels=len(sig.raw_data.get("levels", [])),
        order_id=order_id,
    )
    return ManualTradeResult(
        True, f"{action} {order.quantity} {sig.symbol} @ {fill_price:.2f}",
        sig.symbol, order.quantity, fill_price, order_id,
    )
