"""
Manual trade entry (feature 2).

Lets an operator place a trade by hand from the dashboard — supplying the
symbol, entry, stop, target and quantity directly — bypassing the screener /
grading pipeline but reusing the *exact* same execution path as an automated
entry: a broker bracket order, a journal entry row, and a registered position.
It therefore works identically against the :class:`~execution.broker.PaperBroker`
and the :class:`~execution.broker.IBKRBroker`.

Because the dashboard runs in a separate process from the engine, the default
wiring builds a fresh broker / logger / risk-manager pointed at the same
``DATA_DIR``; all three persist to shared files, so the trade is immediately
visible on the dashboard and reconciled by the engine on its next cycle.

The heavy lifting is in :func:`place_manual_trade`, which is fully injectable
(broker / logger / risk-manager) so it unit-tests without a real broker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import structlog

from config.universe import get_currency
from config.watchlist import WatchlistError, normalize_symbol
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


def validate_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and coerce raw manual-trade parameters.

    Required keys: ``symbol``, ``entry_price``, ``stop_price``, ``target_price``,
    ``quantity``.  Enforces a valid long setup (``stop < entry < target``) and a
    positive integer quantity.

    Raises:
        ManualTradeError: on any invalid / missing field.
    """
    try:
        symbol = normalize_symbol(params.get("symbol", ""))
    except WatchlistError as exc:
        raise ManualTradeError(str(exc)) from exc

    def _num(key: str) -> float:
        raw = params.get(key, None)
        if raw is None or raw == "":
            raise ManualTradeError(f"{key} is required.")
        try:
            return float(raw)
        except (TypeError, ValueError):
            raise ManualTradeError(f"{key} must be a number.") from None

    entry = _num("entry_price")
    stop = _num("stop_price")
    target = _num("target_price")
    try:
        quantity = int(float(params.get("quantity")))
    except (TypeError, ValueError):
        raise ManualTradeError("quantity must be an integer.") from None

    if quantity <= 0:
        raise ManualTradeError("quantity must be a positive integer.")
    if entry <= 0 or stop <= 0 or target <= 0:
        raise ManualTradeError("prices must be positive.")
    if stop >= entry:
        raise ManualTradeError("stop_price must be below entry_price (long only).")
    if target <= entry:
        raise ManualTradeError("target_price must be above entry_price (long only).")

    strategy = str(params.get("strategy", "manual") or "manual").strip().lower()
    return {
        "symbol": symbol,
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "quantity": quantity,
        "strategy": strategy,
    }


def build_manual_order(params: Dict[str, Any]) -> TradeOrder:
    """Build a :class:`TradeOrder` from validated manual-trade *params*."""
    clean = validate_params(params)
    signal = Signal(
        symbol=clean["symbol"],
        strategy=clean["strategy"],
        direction="long",
        entry_price=clean["entry_price"],
        stop_price=clean["stop_price"],
        target_price=clean["target_price"],
        signal_strength=1.0,
        grade=Grade.A,  # manual entries bypass grading; recorded as top grade
        raw_data={"manual": True},
    )
    risk_per_share = clean["entry_price"] - clean["stop_price"]
    risk_amount = round(risk_per_share * clean["quantity"], 2)
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


def place_manual_trade(
    params: Dict[str, Any],
    settings,
    broker: Any = None,
    trade_logger: Any = None,
    risk_manager: Any = None,
) -> ManualTradeResult:
    """Validate *params* and place a manual bracket order.

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

        trade_logger = TradeLogger(str(settings.DATA_DIR))
    if risk_manager is None:
        from risk.manager import RiskManager

        risk_manager = RiskManager(settings)

    sig = order.signal
    try:
        if not broker.is_connected():
            broker.connect()
    except Exception as exc:  # noqa: BLE001
        return ManualTradeResult(False, f"Broker connect failed: {exc}", sig.symbol)

    try:
        result = broker.place_bracket_order(
            symbol=sig.symbol,
            quantity=order.quantity,
            entry_price=sig.entry_price,
            stop_price=sig.stop_price,
            target_price=sig.target_price,
            currency=order.currency,
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

    log.info(
        "manual_trade.placed",
        symbol=sig.symbol, quantity=order.quantity, fill_price=fill_price,
        stop=sig.stop_price, target=sig.target_price, order_id=order_id,
    )
    return ManualTradeResult(
        True, f"Bought {order.quantity} {sig.symbol} @ {fill_price:.2f}",
        sig.symbol, order.quantity, fill_price, order_id,
    )
