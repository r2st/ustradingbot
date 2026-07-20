"""
Walk-forward analysis and parameter optimization on top of the backtester.

The single-shot :class:`backtest.engine.Backtester` answers "how would these
parameters have done over this window?".  Walk-forward analysis answers the
harder, honest question: "if I had *re-optimized* on a rolling training window
and then traded the next window blind, how would I have done out-of-sample?".

Pipeline
--------
1. Slide a ``(train N months, test M months)`` window forward across the full
   date range in ``step`` increments.
2. On each **train** window, grid-search the parameter space and keep the combo
   that maximises the objective metric (in-sample fit).
3. Re-run those winning parameters on the following **test** window — money the
   strategy never saw during fitting (out-of-sample).
4. Aggregate the out-of-sample trades across every window into one honest
   performance record, and compare it against the in-sample fit to flag
   **overfitting** (a large in-sample → out-of-sample degradation).

Everything accepts a pre-loaded ``{symbol: OHLCV}`` dict so it runs offline and
deterministically in tests.  Data is loaded once for the whole span and sliced
per window by the engine's own ``start``/``end`` bounds, so each window keeps
the earlier bars it needs to warm up indicators.
"""

from __future__ import annotations

import dataclasses
import itertools
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from analytics.performance import compute_metrics
from backtest.data import DateLike, load_price_history, to_timestamp
from backtest.engine import BacktestConfig, BacktestTrade, run_backtest
from config.settings import Settings, get_settings

# Config fields that ``BacktestConfig`` owns; every other grid key is treated as
# a ``Settings`` override applied to a copied settings object per combo.
_CONFIG_FIELDS = {f.name for f in dataclasses.fields(BacktestConfig)}


@dataclass
class WalkForwardConfig:
    """Parameters for a walk-forward run.

    Attributes:
        symbols / start / end: Universe and full date span (as for a backtest).
        train_months: Length N of each in-sample training window.
        test_months: Length M of each out-of-sample test window.
        step_months: How far to slide the window each iteration (defaults to
            ``test_months`` — contiguous, non-overlapping test windows).
        param_grid: ``{param_name: [values...]}`` searched on each train window.
            Names matching a ``BacktestConfig`` field tune the config; any other
            name is applied as a ``Settings`` override.  Empty → no optimization
            (the base parameters are used on every window).
        objective: Summary metric maximised when picking the best train combo.
        base: Baseline backtest parameters (strategies, grade, capital, costs).
    """

    symbols: List[str]
    start: DateLike
    end: DateLike
    train_months: int = 12
    test_months: int = 3
    step_months: Optional[int] = None
    param_grid: Dict[str, List[Any]] = field(default_factory=dict)
    objective: str = "sharpe_ratio"
    min_grade: str = "B"
    starting_capital: float = 12_000.0
    strategies: Optional[List[str]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbols": self.symbols,
            "start": str(to_timestamp(self.start).date()),
            "end": str(to_timestamp(self.end).date()),
            "train_months": self.train_months,
            "test_months": self.test_months,
            "step_months": self.step_months or self.test_months,
            "param_grid": self.param_grid,
            "objective": self.objective,
            "min_grade": self.min_grade,
            "starting_capital": self.starting_capital,
            "strategies": self.strategies,
        }


@dataclass
class WalkForwardWindow:
    """One train→test slice of a walk-forward run."""

    index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    best_params: Dict[str, Any]
    in_sample: Dict[str, Any]
    out_of_sample: Dict[str, Any]
    #: Every grid combo's in-sample objective (for the parameter heatmap).
    grid_scores: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class WalkForwardResult:
    """Aggregate output of a walk-forward run."""

    config: WalkForwardConfig
    windows: List[WalkForwardWindow]
    #: Metrics over the concatenation of every window's OOS trades — the number
    #: that actually estimates live performance.
    aggregate_oos: Dict[str, Any]
    #: Metrics over the concatenation of every window's IS trades.
    aggregate_is: Dict[str, Any]
    overfitting: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "windows": [w.to_dict() for w in self.windows],
            "aggregate_oos": self.aggregate_oos,
            "aggregate_is": self.aggregate_is,
            "overfitting": self.overfitting,
        }


# ---------------------------------------------------------------------------
# Window generation
# ---------------------------------------------------------------------------


def generate_windows(
    start: DateLike,
    end: DateLike,
    train_months: int,
    test_months: int,
    step_months: Optional[int] = None,
) -> List[Tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    """Return ``(train_start, train_end, test_start, test_end)`` tuples.

    Windows slide forward by *step_months* (default *test_months*).  Only
    windows whose test period fits entirely within ``[start, end]`` are kept, so
    the caller never evaluates a truncated out-of-sample period.
    """
    if train_months <= 0 or test_months <= 0:
        raise ValueError("train_months and test_months must be positive")
    step = step_months or test_months
    if step <= 0:
        raise ValueError("step_months must be positive")

    s = to_timestamp(start)
    e = to_timestamp(end)
    windows: List[Tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]] = []
    train_start = s
    while True:
        train_end = train_start + pd.DateOffset(months=train_months)
        test_start = train_end
        test_end = test_start + pd.DateOffset(months=test_months)
        if test_end > e + pd.Timedelta(days=1):
            break
        windows.append((train_start, train_end, test_start, test_end))
        train_start = train_start + pd.DateOffset(months=step)
    return windows


# ---------------------------------------------------------------------------
# Parameter application + sweep
# ---------------------------------------------------------------------------


def _apply_params(
    base_config: BacktestConfig,
    base_settings: Settings,
    params: Dict[str, Any],
) -> Tuple[BacktestConfig, Settings]:
    """Split *params* into config overrides and settings overrides."""
    config_overrides = {k: v for k, v in params.items() if k in _CONFIG_FIELDS}
    settings_overrides = {k: v for k, v in params.items() if k not in _CONFIG_FIELDS}
    config = (
        dataclasses.replace(base_config, **config_overrides)
        if config_overrides
        else base_config
    )
    settings = base_settings
    if settings_overrides:
        # Only apply keys the settings model actually defines; unknown keys are
        # ignored rather than raising, so a grid can mix config + settings names.
        valid = {
            k: v
            for k, v in settings_overrides.items()
            if k in type(base_settings).model_fields
        }
        if valid:
            settings = base_settings.model_copy(update=valid)
    return config, settings


def _grid_combinations(param_grid: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
    """Expand a ``{name: [values]}`` grid into a list of param dicts."""
    if not param_grid:
        return [{}]
    names = list(param_grid.keys())
    value_lists = [param_grid[n] for n in names]
    combos: List[Dict[str, Any]] = []
    for values in itertools.product(*value_lists):
        combos.append(dict(zip(names, values)))
    return combos


def parameter_sweep(
    base_config: BacktestConfig,
    param_grid: Dict[str, List[Any]],
    data: Dict[str, pd.DataFrame],
    settings: Optional[Settings] = None,
    objective: str = "sharpe_ratio",
) -> List[Dict[str, Any]]:
    """Grid-search *param_grid* over one window; return one row per combo.

    Each row is ``{"params": {...}, "objective": float, "summary": {...}}``,
    sorted by descending objective (best combo first).  This is the standalone
    parameter-optimization primitive; walk-forward calls it per train window.
    """
    base_settings = settings or get_settings()
    rows: List[Dict[str, Any]] = []
    for params in _grid_combinations(param_grid):
        cfg, sett = _apply_params(base_config, base_settings, params)
        result = run_backtest(cfg, data=data, settings=sett)
        summary = result.summary
        rows.append(
            {
                "params": params,
                "objective": _objective_value(summary, objective),
                "summary": summary,
                "trades": result.trades,
            }
        )
    rows.sort(key=lambda r: r["objective"], reverse=True)
    return rows


def _objective_value(summary: Dict[str, Any], objective: str) -> float:
    """Extract the objective metric from a summary, defaulting to 0."""
    try:
        val = summary.get(objective)
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Overfitting detection
# ---------------------------------------------------------------------------


def detect_overfitting(
    aggregate_is: Dict[str, Any],
    aggregate_oos: Dict[str, Any],
    objective: str = "sharpe_ratio",
) -> Dict[str, Any]:
    """Flag in-sample → out-of-sample degradation.

    The **degradation ratio** is ``1 - oos/is`` on the objective metric: 0 means
    the strategy held up out-of-sample, 1 means it lost all of its in-sample
    edge, and >1 means it flipped negative.  A ratio above 0.5 (edge more than
    halved) or an OOS objective that turned non-positive while IS was positive
    raises the warning.
    """
    is_obj = _objective_value(aggregate_is, objective)
    oos_obj = _objective_value(aggregate_oos, objective)
    if is_obj == 0:
        degradation = 0.0 if oos_obj >= 0 else 1.0
    else:
        degradation = 1.0 - (oos_obj / is_obj)

    reasons: List[str] = []
    if is_obj > 0 and oos_obj <= 0:
        reasons.append(
            f"{objective} collapsed from {is_obj:.3f} (IS) to {oos_obj:.3f} (OOS)"
        )
    if degradation > 0.5:
        reasons.append(f"{objective} degraded {degradation:.0%} out-of-sample")

    is_ret = _objective_value(aggregate_is, "total_return_pct")
    oos_ret = _objective_value(aggregate_oos, "total_return_pct")
    if is_ret > 0 and oos_ret < 0:
        reasons.append(
            f"total return flipped from +{is_ret:.2f}% (IS) to {oos_ret:.2f}% (OOS)"
        )

    return {
        "objective": objective,
        "in_sample_objective": round(is_obj, 4),
        "out_of_sample_objective": round(oos_obj, 4),
        "degradation_ratio": round(degradation, 4),
        "warning": bool(reasons),
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _base_config(cfg: WalkForwardConfig) -> BacktestConfig:
    kwargs: Dict[str, Any] = {
        "symbols": list(cfg.symbols),
        "start": cfg.start,
        "end": cfg.end,
        "min_grade": cfg.min_grade,
        "starting_capital": cfg.starting_capital,
    }
    if cfg.strategies is not None:
        kwargs["strategies"] = list(cfg.strategies)
    return BacktestConfig(**kwargs)


def _summary_from_trades(
    trades: Sequence[BacktestTrade], starting_capital: float
) -> Dict[str, Any]:
    """Compute a metrics summary from raw backtest trades."""
    frame = pd.DataFrame([t.to_record() for t in trades])
    return compute_metrics(frame, starting_capital)


def run_walk_forward(
    config: WalkForwardConfig,
    data: Optional[Dict[str, pd.DataFrame]] = None,
    settings: Optional[Settings] = None,
) -> WalkForwardResult:
    """Run the full walk-forward pipeline and return a :class:`WalkForwardResult`.

    Loads history once for the whole span when *data* is not supplied.
    """
    if data is None:
        data = load_price_history(config.symbols, config.start, config.end)
    base_settings = settings or get_settings()
    base_cfg = _base_config(config)

    windows_spec = generate_windows(
        config.start,
        config.end,
        config.train_months,
        config.test_months,
        config.step_months,
    )

    windows: List[WalkForwardWindow] = []
    all_is_trades: List[BacktestTrade] = []
    all_oos_trades: List[BacktestTrade] = []

    for i, (tr_s, tr_e, te_s, te_e) in enumerate(windows_spec):
        train_cfg = dataclasses.replace(base_cfg, start=tr_s, end=tr_e)
        # 1) Optimize on the train window (grid search).
        sweep = parameter_sweep(
            train_cfg, config.param_grid, data, base_settings, config.objective
        )
        best = sweep[0] if sweep else {"params": {}, "summary": {}, "trades": []}
        best_params = best["params"]
        is_summary = best["summary"]
        all_is_trades.extend(best.get("trades", []))

        # 2) Evaluate the winning params on the test window (out-of-sample).
        test_cfg = dataclasses.replace(base_cfg, start=te_s, end=te_e)
        oos_cfg, oos_sett = _apply_params(test_cfg, base_settings, best_params)
        oos_result = run_backtest(oos_cfg, data=data, settings=oos_sett)
        all_oos_trades.extend(oos_result.trades)

        windows.append(
            WalkForwardWindow(
                index=i,
                train_start=str(tr_s.date()),
                train_end=str(tr_e.date()),
                test_start=str(te_s.date()),
                test_end=str(te_e.date()),
                best_params=best_params,
                in_sample=is_summary,
                out_of_sample=oos_result.summary,
                grid_scores=[
                    {"params": r["params"], "objective": r["objective"]}
                    for r in sweep
                ],
            )
        )

    aggregate_is = _summary_from_trades(all_is_trades, config.starting_capital)
    aggregate_oos = _summary_from_trades(all_oos_trades, config.starting_capital)
    overfitting = detect_overfitting(aggregate_is, aggregate_oos, config.objective)

    return WalkForwardResult(
        config=config,
        windows=windows,
        aggregate_oos=aggregate_oos,
        aggregate_is=aggregate_is,
        overfitting=overfitting,
    )
