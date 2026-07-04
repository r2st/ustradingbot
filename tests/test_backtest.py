"""Tests for the backtester engine and CLI."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from backtest import BacktestConfig, run_backtest
from backtest.engine import Backtester, BacktestResult
from config.settings import Settings


def _make_df(seed: int, n: int = 320, drift: float = 0.0015) -> pd.DataFrame:
    """Synthetic OHLCV with an uptrend + a terminal volume surge."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=datetime(2024, 6, 1), periods=n)
    rets = rng.normal(drift, 0.02, n)
    price = 100 * np.exp(np.cumsum(rets))
    high = price * (1 + np.abs(rng.normal(0, 0.012, n)))
    low = price * (1 - np.abs(rng.normal(0, 0.012, n)))
    op = price * (1 + rng.normal(0, 0.005, n))
    vol = rng.integers(800_000, 3_000_000, n).astype(float)
    vol[-1] = vol[-30:].mean() * 2.5
    return pd.DataFrame(
        {"Open": op, "High": high, "Low": low, "Close": price, "Volume": vol},
        index=dates,
    )


@pytest.fixture
def synthetic_data() -> dict:
    return {sym: _make_df(i) for i, sym in enumerate(["AAA", "BBB", "CCC", "DDD"])}


@pytest.fixture
def bt_config(synthetic_data) -> BacktestConfig:
    df = synthetic_data["AAA"]
    return BacktestConfig(
        symbols=list(synthetic_data),
        start=df.index[250],
        end=df.index[-1],
        min_grade="C",
        starting_capital=12_000.0,
    )


def _run(config, data) -> BacktestResult:
    return run_backtest(config, data=data, settings=Settings())


# ------------------------------------------------------------ basic run


def test_backtest_runs_and_produces_curve(bt_config, synthetic_data) -> None:
    result = _run(bt_config, synthetic_data)
    assert result.equity_curve  # one point per trading day
    assert result.summary["total_trades"] >= 0
    assert result.summary["starting_capital"] == 12_000.0


def test_backtest_equity_reconciles_with_pnl(bt_config, synthetic_data) -> None:
    result = _run(bt_config, synthetic_data)
    # After liquidation, ending equity == starting + summed net P&L.
    expected = 12_000.0 + sum(t.pnl_net for t in result.trades)
    assert result.summary["ending_equity"] == pytest.approx(expected, abs=0.5)


def test_backtest_cash_never_negative(bt_config, synthetic_data) -> None:
    result = _run(bt_config, synthetic_data)
    assert all(point["cash"] >= -0.01 for point in result.equity_curve)


def test_backtest_respects_max_positions(synthetic_data) -> None:
    df = synthetic_data["AAA"]
    config = BacktestConfig(
        symbols=list(synthetic_data),
        start=df.index[250],
        end=df.index[-1],
        min_grade="C",
        max_positions=2,
    )
    result = _run(config, synthetic_data)
    assert max(p["open_positions"] for p in result.equity_curve) <= 2


def test_backtest_stop_and_target_fills_are_realistic(bt_config, synthetic_data) -> None:
    result = _run(bt_config, synthetic_data)
    for trade in result.trades:
        if trade.exit_reason == "STOP_HIT":
            # Filled at or below the stop (slippage/gap-through is adverse).
            assert trade.exit_price <= trade.stop_price + 1e-6
        elif trade.exit_reason == "TARGET_HIT":
            # Filled at or above the target (favourable gaps only).
            assert trade.exit_price >= trade.target_price - 1e-6


def test_backtest_trade_record_has_analytics_fields(bt_config, synthetic_data) -> None:
    result = _run(bt_config, synthetic_data)
    if not result.trades:
        pytest.skip("no trades produced for this seed")
    rec = result.trades[0].to_record()
    for key in ("pnl_net", "pnl_gross", "r_multiple", "strategy", "symbol", "exit_time"):
        assert key in rec
    # net = gross - entry_comm - exit_comm
    assert rec["pnl_net"] == pytest.approx(
        rec["pnl_gross"] - rec["entry_commission"] - rec["exit_commission"], abs=0.01
    )


def test_backtest_min_grade_filter(synthetic_data) -> None:
    """A stricter grade should never produce more trades than a looser one."""
    df = synthetic_data["AAA"]
    base = dict(symbols=list(synthetic_data), start=df.index[250], end=df.index[-1])
    loose = _run(BacktestConfig(min_grade="C", **base), synthetic_data)
    strict = _run(BacktestConfig(min_grade="A", **base), synthetic_data)
    assert len(strict.trades) <= len(loose.trades)


# ------------------------------------------------------------ result plumbing


def test_result_to_dict_and_save(bt_config, synthetic_data, tmp_path) -> None:
    result = _run(bt_config, synthetic_data)
    d = result.to_dict()
    assert set(d) >= {"config", "summary", "equity_curve", "trades", "by_strategy"}

    out = tmp_path / "bt_out"
    result.save(str(out))
    assert (out / "summary.json").exists()
    assert (out / "equity_curve.csv").exists()
    assert (out / "trades.csv").exists()


def test_config_to_dict_serialises_dates(bt_config) -> None:
    d = bt_config.to_dict()
    assert isinstance(d["start"], str)
    assert isinstance(d["end"], str)


def test_backtester_ignores_symbols_without_data() -> None:
    data = {"AAA": _make_df(1)}
    df = data["AAA"]
    config = BacktestConfig(
        symbols=["AAA", "MISSING"], start=df.index[250], end=df.index[-1], min_grade="C"
    )
    # Should not raise despite MISSING having no data.
    result = Backtester(config, data, settings=Settings()).run()
    assert isinstance(result, BacktestResult)


# ------------------------------------------------------------ CLI


def test_cli_parse_helpers() -> None:
    from backtest.__main__ import _parse_strategies, _parse_symbols

    assert _parse_symbols("aapl, msft ,nvda") == ["AAPL", "MSFT", "NVDA"]
    assert _parse_strategies("Momentum,SWING") == ["momentum", "swing"]
    # Defaults when unset.
    assert len(_parse_symbols(None)) > 0
    assert "momentum" in _parse_strategies(None)


def test_cli_build_parser_requires_dates() -> None:
    from backtest.__main__ import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--symbols", "AAPL"])  # missing --start/--end


def test_cli_main_with_injected_run(monkeypatch, tmp_path, capsys) -> None:
    """Drive main() without network by stubbing run_backtest."""
    import backtest.__main__ as cli
    from analytics.performance import compute_metrics

    fake = BacktestResult(
        config=BacktestConfig(symbols=["AAPL"], start="2024-01-01", end="2024-02-01"),
        trades=[],
        equity_curve=[{"date": "2024-01-02", "equity": 12000.0, "cash": 12000.0,
                       "open_positions": 0}],
        summary=compute_metrics(pd.DataFrame(), 12000.0),
        by_strategy=[],
        by_symbol=[],
    )
    monkeypatch.setattr(cli, "run_backtest", lambda config: fake)
    rc = cli.main(["--symbols", "AAPL", "--start", "2024-01-01", "--end", "2024-02-01",
                   "--output", str(tmp_path / "out")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "BACKTEST SUMMARY" in out
    assert (tmp_path / "out" / "summary.json").exists()
