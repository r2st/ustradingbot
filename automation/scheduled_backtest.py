"""
Scheduled nightly backtests (feature 11).

Replays every strategy over a trailing window on the watchlist universe, writes
each run's results under ``DATA_DIR/scheduled_backtests/<date>/`` and emails a
one-line-per-strategy summary.  Invoked by :class:`~automation.scheduler.SimpleScheduler`.

:func:`run_backtests` is injectable (``runner`` / ``data``) so it tests without
fetching price history or hitting the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass
class ScheduledBacktestReport:
    """Summary of a scheduled-backtest sweep."""

    run_date: str
    lookback_days: int
    symbols: int
    per_strategy: List[Dict[str, Any]] = field(default_factory=list)
    output_dir: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_date": self.run_date,
            "lookback_days": self.lookback_days,
            "symbols": self.symbols,
            "per_strategy": self.per_strategy,
            "output_dir": self.output_dir,
            "error": self.error,
        }


def _strategies() -> List[str]:
    from backtest.engine import DEFAULT_STRATEGIES

    return list(DEFAULT_STRATEGIES)


def run_backtests(
    settings,
    today: Optional[date] = None,
    runner: Optional[Callable[[Any], Any]] = None,
    data: Optional[Dict[str, Any]] = None,
    write: bool = True,
) -> ScheduledBacktestReport:
    """Run each strategy over the trailing window.  Never raises.

    Args:
        settings: Application settings.
        today: Reference date (defaults to ``date.today()``).
        runner: ``runner(config) -> BacktestResult``; defaults to
            :func:`backtest.engine.run_backtest`.
        data: Optional pre-loaded price history passed to the default runner
            (keeps tests offline).
        write: Whether to persist each run under ``DATA_DIR/scheduled_backtests``.
    """
    today = today or date.today()
    lookback = int(getattr(settings, "SCHEDULED_BACKTEST_LOOKBACK_DAYS", 180))
    start = today - timedelta(days=lookback)

    try:
        from backtest.engine import BacktestConfig
        from config.watchlist import get_watchlist_store

        symbols = get_watchlist_store(settings.DATA_DIR).scan_symbols()
        if not symbols:
            from config.universe import ALL_SYMBOLS

            symbols = list(ALL_SYMBOLS)
    except Exception as exc:  # noqa: BLE001
        log.warning("scheduled_backtest.setup_failed", error=str(exc))
        return ScheduledBacktestReport(str(today), lookback, 0, error=str(exc))

    if runner is None:
        from backtest.engine import run_backtest

        def _default_runner(cfg):
            return run_backtest(cfg, data=data, settings=settings)

        runner = _default_runner

    out_root = Path(settings.DATA_DIR) / "scheduled_backtests" / str(today)
    per_strategy: List[Dict[str, Any]] = []
    for strategy in _strategies():
        try:
            config = BacktestConfig(
                symbols=symbols,
                start=start,
                end=today,
                strategies=[strategy],
                starting_capital=float(getattr(settings, "TOTAL_CAPITAL", 12_000.0)),
            )
            result = runner(config)
            summary = getattr(result, "summary", {}) or {}
            per_strategy.append({
                "strategy": strategy,
                "total_trades": summary.get("total_trades", 0),
                "win_rate": summary.get("win_rate", 0.0),
                "profit_factor": summary.get("profit_factor"),
                "total_pnl": summary.get("total_pnl", 0.0),
            })
            if write and hasattr(result, "save"):
                result.save(str(out_root / strategy))
        except Exception as exc:  # noqa: BLE001 -- one strategy failing must not abort the sweep
            log.warning("scheduled_backtest.strategy_failed", strategy=strategy, error=str(exc))
            per_strategy.append({"strategy": strategy, "error": str(exc)})

    report = ScheduledBacktestReport(
        run_date=str(today),
        lookback_days=lookback,
        symbols=len(symbols),
        per_strategy=per_strategy,
        output_dir=str(out_root) if write else "",
    )
    return report


def format_summary(report: ScheduledBacktestReport) -> str:
    """Render a plain-text email body for a scheduled-backtest report."""
    lines = [
        f"Nightly Backtest Summary — {report.run_date}",
        "=" * 44,
        f"Universe: {report.symbols} symbols · lookback {report.lookback_days}d",
        "",
    ]
    for row in report.per_strategy:
        if "error" in row:
            lines.append(f"  {row['strategy']:<16} ERROR: {row['error']}")
            continue
        pf = row.get("profit_factor")
        lines.append(
            f"  {str(row['strategy']):<16} trades {row.get('total_trades', 0):>3}  "
            f"win {row.get('win_rate', 0.0) * 100:>5.1f}%  "
            f"PF {pf if pf is not None else 'n/a':<5}  "
            f"P&L ${row.get('total_pnl', 0.0):,.2f}"
        )
    if report.output_dir:
        lines += ["", f"Saved to: {report.output_dir}"]
    return "\n".join(lines)


def run_nightly_backtests(settings, notifier=None) -> ScheduledBacktestReport:
    """Run the sweep and email the summary (used as the scheduler callback)."""
    report = run_backtests(settings)
    body = format_summary(report)
    try:
        if notifier is None:
            from agent.alerts import EmailNotifier

            notifier = EmailNotifier(settings)
        if getattr(notifier, "enabled", False):
            notifier.send_sync(f"Nightly Backtest — {report.run_date}", body)
    except Exception as exc:  # noqa: BLE001
        log.warning("scheduled_backtest.email_failed", error=str(exc))
    log.info("scheduled_backtest.complete", run_date=report.run_date)
    return report
