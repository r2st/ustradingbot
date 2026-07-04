"""
Command-line interface for the backtester.

Examples::

    # One-liner over three symbols and a year of data
    python -m backtest --symbols AAPL,MSFT,NVDA --start 2023-01-01 --end 2024-01-01

    # Full universe, only the momentum family, save artefacts to ./bt_out
    python -m backtest --start 2023-01-01 --end 2024-01-01 \\
        --strategies momentum,vcp_breakout --output bt_out
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

import logging_config
from backtest.engine import DEFAULT_STRATEGIES, BacktestConfig, run_backtest
from config.settings import get_settings
from config.universe import ALL_SYMBOLS


def _parse_symbols(raw: Optional[str]) -> List[str]:
    if not raw:
        return list(ALL_SYMBOLS)
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _parse_strategies(raw: Optional[str]) -> List[str]:
    if not raw:
        return list(DEFAULT_STRATEGIES)
    return [s.strip().lower() for s in raw.split(",") if s.strip()]


def build_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="python -m backtest",
        description="Replay historical daily bars through the trading strategies.",
    )
    parser.add_argument("--symbols", help="Comma-separated tickers (default: full universe)")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD (inclusive)")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD (inclusive)")
    parser.add_argument(
        "--strategies",
        help=f"Comma-separated strategies (default: {','.join(DEFAULT_STRATEGIES)})",
    )
    parser.add_argument("--min-grade", default="B", choices=["A", "B", "C"], help="Minimum grade to trade")
    parser.add_argument(
        "--capital", type=float, default=settings.TOTAL_CAPITAL, help="Starting capital pool"
    )
    parser.add_argument(
        "--slippage-bps", type=float, default=settings.PAPER_SLIPPAGE_BPS,
        help="Entry/stop slippage in basis points",
    )
    parser.add_argument(
        "--commission", type=float, default=settings.PAPER_COMMISSION_PER_SHARE,
        help="Per-share commission",
    )
    parser.add_argument(
        "--max-positions", type=int, default=settings.MAX_OPEN_POSITIONS,
        help="Global open-position cap",
    )
    parser.add_argument("--output", help="Directory to write summary.json / equity_curve.csv / trades.csv")
    parser.add_argument("--log-level", default="WARNING", help="Log level (default WARNING for clean CLI output)")
    return parser


def _print_summary(result) -> None:
    s = result.summary
    print("\n" + "=" * 60)
    print("  BACKTEST SUMMARY")
    print("=" * 60)
    cfg = result.config.to_dict()
    print(f"  Window          : {cfg['start']} → {cfg['end']}")
    print(f"  Symbols         : {len(cfg['symbols'])}   Strategies: {','.join(cfg['strategies'])}")
    print(f"  Starting capital: ${s['starting_capital']:,.2f}")
    print(f"  Ending equity   : ${s['ending_equity']:,.2f}  ({s['total_return_pct']:+.2f}%)")
    print("-" * 60)
    pf = s["profit_factor"]
    pf_str = "∞" if pf is None else f"{pf:.2f}"
    sortino = s["sortino_ratio"]
    sortino_str = "∞" if sortino is None else f"{sortino:.2f}"
    print(f"  Trades          : {s['total_trades']}  (W {s['wins']} / L {s['losses']})")
    print(f"  Win rate        : {s['win_rate'] * 100:.1f}%")
    print(f"  Profit factor   : {pf_str}")
    print(f"  Expectancy      : ${s['expectancy']:,.2f} / trade")
    print(f"  Avg R multiple  : {s['avg_r_multiple']:.2f}R")
    print(f"  Sharpe / Sortino: {s['sharpe_ratio']:.2f} / {sortino_str}")
    print(f"  Max drawdown    : ${s['max_drawdown_abs']:,.2f} ({s['max_drawdown_pct'] * 100:.1f}%)")
    print("-" * 60)
    if result.by_strategy:
        print("  By strategy:")
        for row in result.by_strategy:
            print(f"    {row['strategy']:<16} trades={row['trades']:<4} "
                  f"win={row['win_rate'] * 100:5.1f}%  pnl=${row['total_pnl']:,.2f}")
    print("=" * 60 + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging_config.setup_logging(log_level=args.log_level)

    config = BacktestConfig(
        symbols=_parse_symbols(args.symbols),
        start=args.start,
        end=args.end,
        strategies=_parse_strategies(args.strategies),
        min_grade=args.min_grade,
        starting_capital=args.capital,
        slippage_bps=args.slippage_bps,
        commission_per_share=args.commission,
        max_positions=args.max_positions,
    )

    print(f"Loading data for {len(config.symbols)} symbol(s)…", file=sys.stderr)
    result = run_backtest(config)

    _print_summary(result)

    if args.output:
        result.save(args.output)
        print(f"Artefacts written to {args.output}/", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
