"""
Stop (close) a live position on demand from the dashboard.

Mirrors the manual-trade wiring: the dashboard process builds its own broker /
risk-manager / journal pointed at the shared ``DATA_DIR`` state, closes the
position at market via the broker's ``force_close``, journals the exit, and
removes the position from the risk state — exactly what the exit manager does
for an automated exit, so the trade history and daily P&L stay consistent.

The engine adopts the change on its next loop: ``RiskManager`` re-syncs
``open_positions.json`` each cycle and ``PaperBroker`` reloads
``paper_broker.json`` when another process changed it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

import structlog

from signals.signal_types import ExitEvent, ExitReason

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass
class StopTradeResult:
    """Outcome of a stop-trade (manual close) request."""

    ok: bool
    message: str
    symbol: str = ""
    exit_price: Optional[float] = None
    quantity: Optional[int] = None
    pnl_gross: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "symbol": self.symbol,
            "exit_price": self.exit_price,
            "quantity": self.quantity,
            "pnl_gross": self.pnl_gross,
        }


def _synthesise_exit(symbol: str, pos: Dict[str, Any]) -> ExitEvent:
    """Build an exit event from risk state when the broker has no position.

    Mirrors ``ExitManager._force_exit``'s fallback so a position that exists
    only in the risk state (e.g. broker state lost) can still be cleared.
    """
    from data.fetcher import fetch_current_price

    entry = float(pos.get("entry_price", 0) or 0)
    qty = int(float(pos.get("quantity", 0) or 0))
    side = str(pos.get("direction", "long") or "long").lower()
    price = fetch_current_price(symbol)
    if price is None:
        price = entry
    pnl = (entry - price) * qty if side == "short" else (price - entry) * qty
    return ExitEvent(
        symbol=symbol,
        exit_price=round(float(price), 4),
        exit_reason=ExitReason.MANUAL,
        exit_date=datetime.now(),
        pnl_gross=round(pnl, 2),
        fill_details={"quantity": qty, "commission": 0.0},
    )


def stop_open_position(
    symbol: str,
    settings,
    broker: Any = None,
    risk_manager: Any = None,
    trade_logger: Any = None,
) -> StopTradeResult:
    """Close the open position in *symbol* at the current market price.

    Args:
        symbol: Ticker of the position to stop.
        settings: Application settings (used to build the default wiring).
        broker / risk_manager / trade_logger: Injected collaborators; when
            ``None`` fresh instances pointed at ``settings.DATA_DIR`` are
            built (the manual-trade pattern).

    Returns:
        A :class:`StopTradeResult`; failures return ``ok=False`` with a
        user-facing message rather than raising.
    """
    symbol = str(symbol or "").strip().upper()
    if not symbol:
        return StopTradeResult(False, "A symbol is required.")

    if broker is None:
        from execution.broker import make_broker

        broker = make_broker(settings)
    if risk_manager is None:
        from risk.manager import RiskManager

        risk_manager = RiskManager(settings)
    if trade_logger is None:
        from journal.trade_logger import TradeLogger

        trade_logger = TradeLogger(str(settings.DATA_DIR), trading_mode=settings.TRADING_MODE)

    try:
        risk_manager.sync_positions_from_disk()
    except Exception:  # noqa: BLE001 -- stale risk state must not block a close
        log.debug("stop_trade.sync_failed", symbol=symbol, exc_info=True)
    pos = risk_manager.get_open_positions().get(symbol)

    try:
        if not broker.is_connected():
            broker.connect()
    except Exception as exc:  # noqa: BLE001
        return StopTradeResult(False, f"Broker connect failed: {exc}", symbol)

    try:
        event: Optional[ExitEvent] = broker.force_close(
            symbol, ExitReason.MANUAL
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("stop_trade.broker_error", symbol=symbol, error=str(exc))
        return StopTradeResult(False, f"Broker error: {exc}", symbol)

    if event is None:
        if pos is None:
            return StopTradeResult(
                False, f"No open position found for {symbol}.", symbol
            )
        event = _synthesise_exit(symbol, pos)

    exit_commission = float(event.fill_details.get("commission", 0.0) or 0.0)
    quantity = int(event.fill_details.get("quantity", 0) or 0)
    if not quantity and pos is not None:
        quantity = int(float(pos.get("quantity", 0) or 0))

    # Journal + de-register exactly like the exit manager's finalise step.
    try:
        trade_logger.log_exit(symbol, event, exit_commission=exit_commission)
    except Exception:  # noqa: BLE001 -- the close happened; keep going
        log.error("stop_trade.journal_error", symbol=symbol, exc_info=True)
    try:
        if pos is not None:
            risk_manager.remove_position(symbol, event)
        risk_manager.record_daily_pnl(event.pnl_gross - exit_commission)
    except Exception:  # noqa: BLE001
        log.error("stop_trade.risk_state_error", symbol=symbol, exc_info=True)

    log.info(
        "stop_trade.closed",
        symbol=symbol,
        exit_price=event.exit_price,
        quantity=quantity,
        pnl_gross=event.pnl_gross,
    )
    sign = "+" if event.pnl_gross >= 0 else ""
    return StopTradeResult(
        ok=True,
        message=(
            f"Stopped {symbol}: closed {quantity} shares at "
            f"${event.exit_price:.2f} ({sign}${event.pnl_gross:.2f} gross). "
            "The engine will not re-manage this position."
        ),
        symbol=symbol,
        exit_price=event.exit_price,
        quantity=quantity,
        pnl_gross=event.pnl_gross,
    )
