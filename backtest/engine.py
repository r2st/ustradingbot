"""
Event-driven daily-bar backtester for the US Trading Bot.

Replays historical daily bars through the *same* strategy detectors the live
bot uses (``signals.*``) and the *same* fill maths as the paper broker
(``execution.broker`` slippage / gap / commission helpers), so a backtest and a
live paper run of a signal produce identical fills.

How it works
------------
For every trading day ``t`` (the union of all symbols' bar dates within the
window), in order:

1. **Exits** — each open position is checked against day ``t``'s bar: stop hit
   (with gap-through), target hit, or a per-strategy max-hold time exit.
2. **Entries** — every flat symbol that traded on ``t`` is scored by the
   detectors using history *up to and including* ``t``.  Qualifying signals are
   ranked by strength and filled at the day's close (plus slippage), subject to
   position caps and available cash.
3. **Mark-to-market** — portfolio equity (cash + open positions at the close)
   is recorded, producing the daily equity curve.

Sizing mirrors :class:`risk.manager.RiskManager` (risk-per-trade %, 10 %
notional cap, Grade-B 0.75× haircut, mean-reversion 0.5× haircut) against a
single starting-capital pool.  Results are scored with :mod:`analytics`, so the
summary statistics match the live journal's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
import structlog

from analytics.performance import breakdown_by, compute_metrics
from backtest.data import DateLike, load_price_history, to_timestamp
from config.settings import Settings, get_settings
from config.universe import get_currency
from execution.broker import (
    apply_entry_slippage,
    commission_for,
    stop_exit_fill,
    target_exit_fill,
)
from signals.combined_filter import score_symbol
from signals.mean_reversion_signal import detect as detect_mean_reversion
from signals.pead_signal import detect as detect_pead
from signals.signal_types import Grade, Signal
from signals.vcp_signal import detect as detect_vcp

log = structlog.get_logger(__name__)

# Detector dispatch — mirrors signals.screener but callable per configured set.
_DEDICATED_DETECTORS: Dict[str, Callable[[str, pd.DataFrame], Optional[Signal]]] = {
    "vcp_breakout": detect_vcp,
    "pead": detect_pead,
    "mean_reversion": detect_mean_reversion,
}

# Per-strategy maximum hold (calendar/trading days) — mirrors the exit manager.
_MAX_HOLD_DAYS: Dict[str, int] = {
    "momentum": 20,
    "vcp_breakout": 25,
    "swing": 15,
    "pead": 30,
    "mean_reversion": 7,
}

# Strategies run by default: the technical detectors that need no live feed.
# PEAD is excluded by default because it depends on an earnings calendar.
DEFAULT_STRATEGIES: List[str] = ["vcp_breakout", "momentum", "swing", "mean_reversion"]

_MOMENTUM_FAMILY = frozenset({"momentum", "vcp_breakout"})


def _family(strategy: str) -> str:
    s = strategy.lower()
    return "momentum" if s in _MOMENTUM_FAMILY else s


# ---------------------------------------------------------------------------
# Config / result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class BacktestConfig:
    """Parameters for a backtest run.

    Attributes:
        symbols: Universe to test.
        start: First trading day (inclusive).
        end: Last trading day (inclusive).
        strategies: Strategy names to run (subset of the five).
        min_grade: Minimum signal grade to trade (``"A"``/``"B"``/``"C"``).
        starting_capital: Single capital pool the sim sizes against.
        slippage_bps: Entry/stop slippage in basis points.
        commission_per_share: Per-share commission on entry and exit.
        max_positions: Global open-position cap.
    """

    symbols: List[str]
    start: DateLike
    end: DateLike
    strategies: List[str] = field(default_factory=lambda: list(DEFAULT_STRATEGIES))
    min_grade: str = "B"
    starting_capital: float = 12_000.0
    slippage_bps: float = 5.0
    commission_per_share: float = 0.005
    max_positions: int = 25

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbols": self.symbols,
            "start": str(to_timestamp(self.start).date()),
            "end": str(to_timestamp(self.end).date()),
            "strategies": self.strategies,
            "min_grade": self.min_grade,
            "starting_capital": self.starting_capital,
            "slippage_bps": self.slippage_bps,
            "commission_per_share": self.commission_per_share,
            "max_positions": self.max_positions,
        }


@dataclass
class BacktestTrade:
    """A single completed round-trip trade produced by the backtest."""

    symbol: str
    strategy: str
    currency: str
    quantity: int
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: pd.Timestamp
    exit_price: float
    exit_reason: str
    stop_price: float
    target_price: float
    pnl_gross: float
    entry_commission: float
    exit_commission: float
    pnl_net: float
    r_multiple: float
    bars_held: int

    def to_record(self) -> Dict[str, Any]:
        """Return an analytics-compatible dict (matches the journal schema)."""
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "currency": self.currency,
            "quantity": self.quantity,
            "entry_time": self.entry_date.isoformat(),
            "entry_fill_price": round(self.entry_price, 4),
            "exit_time": self.exit_date.isoformat(),
            "exit_price": round(self.exit_price, 4),
            "exit_reason": self.exit_reason,
            "stop_price": round(self.stop_price, 4),
            "target_price": round(self.target_price, 4),
            "pnl_gross": round(self.pnl_gross, 2),
            "entry_commission": round(self.entry_commission, 4),
            "exit_commission": round(self.exit_commission, 4),
            "pnl_net": round(self.pnl_net, 2),
            "r_multiple": round(self.r_multiple, 4),
            "bars_held": self.bars_held,
        }


@dataclass
class _OpenPosition:
    symbol: str
    strategy: str
    currency: str
    quantity: int
    entry_price: float
    entry_commission: float
    stop_price: float
    target_price: float
    original_stop: float
    entry_date: pd.Timestamp
    bars_held: int = 0


@dataclass
class BacktestResult:
    """Full output of a backtest run."""

    config: BacktestConfig
    trades: List[BacktestTrade]
    equity_curve: List[Dict[str, Any]]
    summary: Dict[str, Any]
    by_strategy: List[Dict[str, Any]]
    by_symbol: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "summary": self.summary,
            "by_strategy": self.by_strategy,
            "by_symbol": self.by_symbol,
            "equity_curve": self.equity_curve,
            "trades": [t.to_record() for t in self.trades],
        }

    def trades_frame(self) -> pd.DataFrame:
        """Return the trades as an analytics-ready DataFrame."""
        return pd.DataFrame([t.to_record() for t in self.trades])

    def save(self, output_dir: str) -> None:
        """Write ``summary.json``, ``equity_curve.csv``, and ``trades.csv``."""
        import json
        from pathlib import Path

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "summary.json").write_text(
            json.dumps(
                {
                    "config": self.config.to_dict(),
                    "summary": self.summary,
                    "by_strategy": self.by_strategy,
                    "by_symbol": self.by_symbol,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        pd.DataFrame(self.equity_curve).to_csv(out / "equity_curve.csv", index=False)
        self.trades_frame().to_csv(out / "trades.csv", index=False)


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------

_GRADE_RANK = {"A": 0, "B": 1, "C": 2, "F": 3}


class Backtester:
    """Runs a single backtest over pre-loaded price data."""

    def __init__(
        self,
        config: BacktestConfig,
        data: Dict[str, pd.DataFrame],
        settings: Optional[Settings] = None,
    ) -> None:
        self._config = config
        self._settings = settings or get_settings()
        self._start = to_timestamp(config.start)
        self._end = to_timestamp(config.end)

        # Keep only symbols we actually have data for, normalising the index.
        self._data: Dict[str, pd.DataFrame] = {}
        for sym, df in data.items():
            if df is None or df.empty:
                continue
            frame = df.sort_index()
            if isinstance(frame.index, pd.DatetimeIndex) and frame.index.tz:
                frame.index = frame.index.tz_localize(None)
            self._data[sym] = frame

        self._cash = float(config.starting_capital)
        self._positions: Dict[str, _OpenPosition] = {}
        self._last_close: Dict[str, float] = {}
        self._cooldown_until: Dict[str, pd.Timestamp] = {}
        self._trades: List[BacktestTrade] = []
        self._equity_curve: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ run

    def run(self) -> BacktestResult:
        """Execute the backtest and return a :class:`BacktestResult`."""
        trading_dates = self._trading_dates()
        log.info(
            "backtest.start",
            symbols=len(self._data),
            days=len(trading_dates),
            strategies=self._config.strategies,
        )

        for t in trading_dates:
            self._update_last_closes(t)
            self._process_exits(t)
            self._process_entries(t)
            self._record_equity(t)

        # Close any positions still open at the end of the window at last close.
        self._liquidate_open(trading_dates[-1] if trading_dates else self._end)

        return self._build_result()

    # -------------------------------------------------------------- dates

    def _trading_dates(self) -> List[pd.Timestamp]:
        """Sorted union of all symbols' bar dates within [start, end]."""
        dates: set[pd.Timestamp] = set()
        for df in self._data.values():
            in_window = df.index[(df.index >= self._start) & (df.index <= self._end)]
            dates.update(in_window)
        return sorted(dates)

    def _update_last_closes(self, t: pd.Timestamp) -> None:
        for sym, df in self._data.items():
            if t in df.index:
                self._last_close[sym] = float(df.at[t, "Close"])

    # -------------------------------------------------------------- exits

    def _process_exits(self, t: pd.Timestamp) -> None:
        for sym in list(self._positions.keys()):
            df = self._data.get(sym)
            if df is None or t not in df.index:
                continue
            pos = self._positions[sym]
            pos.bars_held += 1
            bar = df.loc[t]
            bar_open = float(bar["Open"])
            bar_high = float(bar["High"])
            bar_low = float(bar["Low"])
            bar_close = float(bar["Close"])
            slip = self._config.slippage_bps

            exit_price: Optional[float] = None
            reason: Optional[str] = None

            # Stop assumed to fill first when both touched (conservative).
            if bar_low <= pos.stop_price:
                exit_price = stop_exit_fill(pos.stop_price, bar_open, slip)
                reason = "STOP_HIT"
            elif bar_high >= pos.target_price:
                exit_price = target_exit_fill(pos.target_price, bar_open)
                reason = "TARGET_HIT"
            else:
                max_hold = _MAX_HOLD_DAYS.get(
                    pos.strategy.lower(), self._settings.HOLD_MAX_DAYS
                )
                if pos.bars_held >= max_hold:
                    # Time exit at the close, treated as a market order.
                    exit_price = round(bar_close * (1.0 - slip / 10_000.0), 4)
                    reason = (
                        "TIME_EXIT_LOSS"
                        if bar_close < pos.entry_price
                        else "TIME_EXIT_FLAT"
                    )

            if exit_price is not None and reason is not None:
                self._close_position(sym, t, exit_price, reason)

    def _close_position(
        self, sym: str, t: pd.Timestamp, exit_price: float, reason: str
    ) -> None:
        pos = self._positions.pop(sym)
        exit_commission = commission_for(pos.quantity, self._config.commission_per_share)
        self._cash += exit_price * pos.quantity - exit_commission

        pnl_gross = (exit_price - pos.entry_price) * pos.quantity
        pnl_net = pnl_gross - pos.entry_commission - exit_commission
        risk_per_share = pos.entry_price - pos.original_stop
        r_multiple = (
            (exit_price - pos.entry_price) / risk_per_share
            if risk_per_share > 0
            else 0.0
        )

        self._trades.append(
            BacktestTrade(
                symbol=sym,
                strategy=pos.strategy,
                currency=pos.currency,
                quantity=pos.quantity,
                entry_date=pos.entry_date,
                entry_price=pos.entry_price,
                exit_date=t,
                exit_price=exit_price,
                exit_reason=reason,
                stop_price=pos.stop_price,
                target_price=pos.target_price,
                pnl_gross=pnl_gross,
                entry_commission=pos.entry_commission,
                exit_commission=exit_commission,
                pnl_net=pnl_net,
                r_multiple=r_multiple,
                bars_held=pos.bars_held,
            )
        )

        # Re-entry cooldown: block same-symbol re-entry for a day after a
        # stop-out (mirrors the live long-cooldown intent on a daily grid).
        if reason in ("STOP_HIT", "SETUP_BROKEN"):
            self._cooldown_until[sym] = t + pd.Timedelta(days=1)

    def _liquidate_open(self, t: pd.Timestamp) -> None:
        for sym in list(self._positions.keys()):
            price = self._last_close.get(sym, self._positions[sym].entry_price)
            self._close_position(sym, t, round(float(price), 4), "TIME_EXIT_FLAT")

    # ------------------------------------------------------------- entries

    def _process_entries(self, t: pd.Timestamp) -> None:
        if len(self._positions) >= self._config.max_positions:
            return

        candidates: List[Signal] = []
        for sym, df in self._data.items():
            if sym in self._positions:
                continue
            if t not in df.index:
                continue
            cd = self._cooldown_until.get(sym)
            if cd is not None and t < cd:
                continue
            history = df.loc[:t]
            if len(history) < self._settings.MIN_OHLCV_ROWS:
                continue
            signal = self._detect(sym, history)
            if signal is not None:
                candidates.append(signal)

        # Strongest signals first.
        candidates.sort(key=lambda s: s.signal_strength, reverse=True)
        for signal in candidates:
            if len(self._positions) >= self._config.max_positions:
                break
            self._try_enter(signal, t)

    def _detect(self, symbol: str, history: pd.DataFrame) -> Optional[Signal]:
        """Run configured strategies in priority order; return first qualifier."""
        for strategy in self._config.strategies:
            detector = _DEDICATED_DETECTORS.get(strategy)
            try:
                signal = (
                    detector(symbol, history)
                    if detector is not None
                    else score_symbol(symbol, strategy, history)
                )
            except Exception:  # noqa: BLE001 -- a bad symbol never aborts the run
                log.exception("backtest.detect_error", symbol=symbol, strategy=strategy)
                continue
            if signal is None:
                continue
            if self._grade_ok(signal.grade):
                return signal
        return None

    def _grade_ok(self, grade: Grade) -> bool:
        return _GRADE_RANK.get(grade.value, 9) <= _GRADE_RANK.get(
            self._config.min_grade.upper(), 9
        )

    def _try_enter(self, signal: Signal, t: pd.Timestamp) -> None:
        if not self._strategy_has_room(signal.strategy):
            return
        risk_per_share = signal.entry_price - signal.stop_price
        if risk_per_share <= 0 or signal.entry_price <= 0:
            return

        shares = self._size(signal)
        if shares <= 0:
            return

        fill_price = apply_entry_slippage(signal.entry_price, self._config.slippage_bps)
        commission = commission_for(shares, self._config.commission_per_share)
        cost = fill_price * shares + commission

        # Down-size to fit available cash rather than skipping outright.
        if cost > self._cash:
            affordable = int((self._cash - commission) / fill_price) if fill_price else 0
            shares = min(shares, max(0, affordable))
            if shares <= 0:
                return
            commission = commission_for(shares, self._config.commission_per_share)
            cost = fill_price * shares + commission
            if cost > self._cash:
                return

        self._cash -= cost
        self._positions[signal.symbol] = _OpenPosition(
            symbol=signal.symbol,
            strategy=signal.strategy,
            currency=get_currency(signal.symbol),
            quantity=shares,
            entry_price=fill_price,
            entry_commission=commission,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
            original_stop=signal.stop_price,
            entry_date=t,
        )

    def _size(self, signal: Signal) -> int:
        """Position size mirroring RiskManager.build_order (single pool)."""
        capital = self._config.starting_capital
        strategy_mod = 0.5 if signal.strategy.lower() == "mean_reversion" else 1.0
        max_risk = capital * self._settings.MAX_POSITION_SIZE_PCT * strategy_mod
        risk_per_share = signal.entry_price - signal.stop_price
        shares = int(max_risk / risk_per_share)
        # Notional cap: no single position exceeds 10% of the pool.
        notional_cap = int(capital * 0.10 / signal.entry_price)
        shares = min(shares, notional_cap)
        if signal.grade == Grade.B:
            shares = int(shares * 0.75)
        return max(0, shares)

    def _strategy_has_room(self, strategy: str) -> bool:
        family = _family(strategy)
        count = sum(
            1 for p in self._positions.values() if _family(p.strategy) == family
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
            return True
        return count < cap

    # --------------------------------------------------------------- equity

    def _record_equity(self, t: pd.Timestamp) -> None:
        holdings = sum(
            pos.quantity * self._last_close.get(sym, pos.entry_price)
            for sym, pos in self._positions.items()
        )
        equity = self._cash + holdings
        self._equity_curve.append(
            {
                "date": str(t.date()),
                "equity": round(equity, 2),
                "cash": round(self._cash, 2),
                "open_positions": len(self._positions),
            }
        )

    # --------------------------------------------------------------- result

    def _build_result(self) -> BacktestResult:
        records = [t.to_record() for t in self._trades]
        trades_df = pd.DataFrame(records)
        summary = compute_metrics(trades_df, self._config.starting_capital)
        by_strategy = breakdown_by(trades_df, "strategy", self._config.starting_capital)
        by_symbol = breakdown_by(trades_df, "symbol", self._config.starting_capital)
        log.info(
            "backtest.complete",
            trades=len(self._trades),
            total_pnl=summary.get("total_pnl"),
            win_rate=summary.get("win_rate"),
        )
        return BacktestResult(
            config=self._config,
            trades=self._trades,
            equity_curve=self._equity_curve,
            summary=summary,
            by_strategy=by_strategy,
            by_symbol=by_symbol,
        )


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------


def run_backtest(
    config: BacktestConfig,
    data: Optional[Dict[str, pd.DataFrame]] = None,
    settings: Optional[Settings] = None,
) -> BacktestResult:
    """Run a backtest, loading data from Yahoo Finance if not provided.

    Args:
        config: The backtest parameters.
        data: Optional pre-loaded ``{symbol: OHLCV DataFrame}`` (used by tests
            and for offline replay).  When ``None``, history is fetched.
        settings: Optional settings override (defaults to the singleton).

    Returns:
        A populated :class:`BacktestResult`.
    """
    if data is None:
        data = load_price_history(config.symbols, config.start, config.end)
    return Backtester(config, data, settings=settings).run()
