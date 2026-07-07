"""
Short-scan orchestrator — mirrors :mod:`signals.screener` for the short side.

Per symbol the detectors run in :data:`~short_strategies.strategies.STRATEGY_PRIORITY`
order (first qualifying signal wins).  The ADX confirmation filter gates the
trend-following setups when enabled.  Candidates then pass through the shared
:class:`~short_strategies.risk.filters.ShortFilterChain`, strongest first, so
the portfolio short-exposure cap admits the best setups.

Usage::

    from short_strategies import run_short_scan
    signals = run_short_scan(symbols, min_grade="B",
                             open_positions=risk.get_open_positions())
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
import structlog

from config.settings import get_settings
from config.universe import get_sector
from data.fetcher import fetch_ohlcv
from short_strategies.common.config import ShortModuleConfig, get_short_config
from short_strategies.common.context import MarketContext, build_market_context
from short_strategies.common.signal import ShortSignal
from short_strategies.risk.filters import ShortFilterChain
from short_strategies.strategies import (
    DETECTORS,
    STRATEGY_PRIORITY,
    TREND_FOLLOWING,
    adx_filter,
)
from signals.signal_types import Grade, Signal

log = structlog.get_logger(__name__)

_GRADE_RANK = {"A": 0, "B": 1, "C": 2, "F": 3}

#: Strategies that need the cross-sectional MarketContext to fire.
_CONTEXT_STRATEGIES = frozenset(
    {"short_relative_weakness", "short_laggard_fade"}
)


def _grade_meets_minimum(grade: Grade, min_grade: str) -> bool:
    return _GRADE_RANK.get(grade.value, 99) <= _GRADE_RANK.get(min_grade.upper(), 99)


def _config_for(cfg: ShortModuleConfig, strategy_id: str):
    """Return the per-strategy config dataclass for *strategy_id*."""
    return getattr(cfg, strategy_id.removeprefix("short_"), None)


def _scan_symbol(
    symbol: str,
    df: pd.DataFrame,
    cfg: ShortModuleConfig,
    ctx: MarketContext,
    min_grade: str,
    allowed_strategies: Optional[List[str]],
) -> Optional[ShortSignal]:
    """Try each short strategy for one symbol; first qualifying wins."""
    strategies = [
        s for s in STRATEGY_PRIORITY
        if allowed_strategies is None or s in allowed_strategies
    ]
    for strategy_id in strategies:
        detector = DETECTORS[strategy_id]
        sig = detector(
            symbol, df,
            config=_config_for(cfg, strategy_id),
            filters=cfg.filters,
            ctx=ctx if strategy_id in _CONTEXT_STRATEGIES else None,
        )
        if sig is None:
            continue

        # Strategy 4: ADX trend-strength confirmation over trend followers.
        if cfg.adx_confirm_enabled and strategy_id in TREND_FOLLOWING:
            if not adx_filter.confirms_downtrend(df, cfg.adx_filter):
                log.debug("short_scan.adx_veto", symbol=symbol,
                          strategy=strategy_id)
                continue
            sig.filters_passed.append("adx_confirm")

        grade = Grade.from_score(sig.signal_strength)
        if not _grade_meets_minimum(grade, min_grade):
            log.debug(
                "short_scan.grade_below_minimum",
                symbol=symbol, strategy=strategy_id, grade=grade.value,
            )
            continue
        return sig
    return None


def run_short_scan(
    symbols: List[str],
    min_grade: str = "B",
    allowed_strategies: Optional[List[str]] = None,
    open_positions: Optional[Dict[str, Dict[str, Any]]] = None,
    config: Optional[ShortModuleConfig] = None,
    fetch: Callable[..., Optional[pd.DataFrame]] = fetch_ohlcv,
    filter_chain: Optional[ShortFilterChain] = None,
) -> List[Signal]:
    """Scan *symbols* for short setups and return pipeline-native signals.

    Args:
        symbols: Universe to scan.
        min_grade: Minimum acceptable grade letter (default ``"B"``).
        allowed_strategies: Optional whitelist of short strategy ids (the
            dashboard trade selection passes long strategy names through
            unchanged; ids not in the short registry simply never match).
        open_positions: Risk-manager position dict for the exposure cap.
        config: Module config override (defaults to :func:`get_short_config`).
        fetch: OHLCV fetcher (injectable for tests/backtests).
        filter_chain: Filter-chain override (tests inject offline lookups).

    Returns:
        List of core :class:`Signal` objects (``direction="short"``), sorted
        strongest first.  Empty when the module is disabled.
    """
    cfg = config or get_short_config()
    if not cfg.enabled:
        return []
    settings = get_settings()
    start = time.monotonic()

    log.info("short_scan.start", total_symbols=len(symbols), min_grade=min_grade)

    # Fetch once per symbol (the fetcher's TTL cache makes re-fetches cheap
    # when the long scan already pulled the same frames this cycle).
    frames: Dict[str, pd.DataFrame] = {}
    errors = 0
    for sym in symbols:
        try:
            df = fetch(sym, period=settings.OHLCV_FETCH_PERIOD)
            if df is not None and len(df) >= settings.MIN_OHLCV_ROWS:
                frames[sym] = df
        except Exception:
            errors += 1
            log.exception("short_scan.fetch_error", symbol=sym)

    benchmark_df: Optional[pd.DataFrame] = None
    try:
        benchmark_df = fetch(settings.REGIME_BENCHMARK)
    except Exception:
        log.warning("short_scan.benchmark_fetch_failed")

    ctx = build_market_context(
        list(frames.keys()),
        frames,
        benchmark_df,
        cfg.relative_weakness.rank_lookback_days,
        get_sector,
    )

    chain = filter_chain or ShortFilterChain(
        cfg.filters,
        total_capital=settings.TOTAL_CAPITAL,
        data_dir=settings.DATA_DIR,
        max_position_size_pct=settings.MAX_POSITION_SIZE_PCT,
        risk_modifier=cfg.risk_modifier,
    )

    # Detect per symbol, then filter strongest-first so the exposure cap
    # admits the best candidates.
    candidates: List[ShortSignal] = []
    for sym, df in frames.items():
        try:
            sig = _scan_symbol(sym, df, cfg, ctx, min_grade, allowed_strategies)
            if sig is not None:
                candidates.append(sig)
        except Exception:
            errors += 1
            log.exception("short_scan.symbol_error", symbol=sym)
    candidates.sort(key=lambda s: s.signal_strength, reverse=True)

    accepted: List[Signal] = []
    accepted_notional = 0.0
    for sig in candidates:
        ok, _reason = chain.apply(sig, open_positions, accepted_notional)
        if not ok:
            continue
        accepted_notional += chain.estimate_notional(sig)
        accepted.append(sig.to_core_signal())

    elapsed = time.monotonic() - start
    log.info(
        "short_scan.complete",
        total_symbols=len(symbols),
        candidates=len(candidates),
        signals_found=len(accepted),
        errors=errors,
        elapsed_seconds=round(elapsed, 2),
    )
    return accepted
