"""
Selective-scan orchestrator — mirrors :mod:`signals.screener` for the
highly selective strategy family.

Per symbol the detectors run in :data:`~selective_strategies.strategies.STRATEGY_PRIORITY`
order (first qualifying signal wins).  Candidates are sorted strongest-first
so the best setups surface at the top of the signal list.

Usage::

    from selective_strategies import run_selective_scan
    signals = run_selective_scan(symbols, min_grade="B")
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, List, Optional

import pandas as pd
import structlog

from data.fetcher import fetch_ohlcv
from selective_strategies.config import SelectiveConfig, get_selective_config
from selective_strategies.signal import SelectiveSignal
from selective_strategies.strategies import DETECTORS, STRATEGY_PRIORITY
from signals.signal_types import Grade, Signal

log = structlog.get_logger(__name__)

_GRADE_RANK = {"A": 0, "B": 1, "C": 2, "F": 3}

# Map strategy IDs to SelectiveConfig field names (strip the "hs_" prefix).
_FIELD_MAP = {
    "hs_rsi2_reversal": "rsi2_reversal",
    "hs_triple_timeframe": "triple_timeframe",
    "hs_bb_climax": "bb_climax",
    "hs_pead_drift": "pead_drift",
    "hs_gap_fill": "gap_fill",
    "hs_turnaround_tuesday": "turnaround_tuesday",
}


def _grade_meets_minimum(grade: Grade, min_grade: str) -> bool:
    """Return ``True`` if *grade* is at least as good as *min_grade*."""
    return _GRADE_RANK.get(grade.value, 99) <= _GRADE_RANK.get(min_grade.upper(), 99)


def _config_for(cfg: SelectiveConfig, strategy_id: str):
    """Get per-strategy config from the master config."""
    field_name = _FIELD_MAP.get(strategy_id)
    if field_name:
        return getattr(cfg, field_name, None)
    return None


def _scan_symbol(
    symbol: str,
    df: pd.DataFrame,
    cfg: SelectiveConfig,
    min_grade: str,
    allowed_strategies: Optional[List[str]],
) -> Optional[Signal]:
    """Try each selective strategy for one symbol; first qualifying wins."""
    strategies = [
        s for s in STRATEGY_PRIORITY
        if allowed_strategies is None or s in allowed_strategies
    ]
    for strategy_id in strategies:
        detector = DETECTORS[strategy_id]
        strategy_cfg = _config_for(cfg, strategy_id)
        sig: Optional[SelectiveSignal] = detector(
            symbol, df, config=strategy_cfg,
        )
        if sig is None:
            continue

        grade = Grade.from_score(sig.signal_strength)
        if not _grade_meets_minimum(grade, min_grade):
            log.debug(
                "selective_scan.grade_below_minimum",
                symbol=symbol,
                strategy=strategy_id,
                grade=grade.value,
            )
            continue

        log.debug(
            "selective_scan.hit",
            symbol=symbol,
            strategy=strategy_id,
            grade=grade.value,
            signal_strength=sig.signal_strength,
        )
        return sig.to_core_signal()

    return None


def run_selective_scan(
    symbols: List[str],
    min_grade: str = "B",
    allowed_strategies: Optional[List[str]] = None,
    config: Optional[SelectiveConfig] = None,
    fetch: Callable[..., Optional[pd.DataFrame]] = fetch_ohlcv,
    max_workers: int = 8,
) -> List[Signal]:
    """Scan *symbols* for highly selective setups and return core signals.

    Args:
        symbols: Universe to scan.
        min_grade: Minimum acceptable grade letter (default ``"B"``).
        allowed_strategies: Optional whitelist of selective strategy ids;
            ``None`` runs every strategy.
        config: Module config override (defaults to :func:`get_selective_config`).
        fetch: OHLCV fetcher (injectable for tests/backtests).
        max_workers: Thread pool size for parallel scanning.

    Returns:
        List of core :class:`Signal` objects sorted by ``signal_strength``
        descending.  Empty when the module is disabled.
    """
    cfg = config or get_selective_config()
    if not cfg.enabled:
        return []

    start = time.monotonic()
    log.info(
        "selective_scan.start",
        total_symbols=len(symbols),
        min_grade=min_grade,
        max_workers=max_workers,
    )

    # ------------------------------------------------------------------
    # Fetch OHLCV once per symbol (period="1y" for SMA-200 coverage)
    # ------------------------------------------------------------------
    frames = {}
    errors = 0
    for sym in symbols:
        try:
            df = fetch(sym, period="1y")
            if df is not None and len(df) >= 10:
                frames[sym] = df
        except Exception:
            errors += 1
            log.exception("selective_scan.fetch_error", symbol=sym)

    # ------------------------------------------------------------------
    # Detect per symbol (parallel when enough symbols)
    # ------------------------------------------------------------------
    signals: List[Signal] = []

    if max_workers <= 1 or len(frames) <= 3:
        for sym, df in frames.items():
            try:
                sig = _scan_symbol(sym, df, cfg, min_grade, allowed_strategies)
                if sig is not None:
                    signals.append(sig)
            except Exception:
                errors += 1
                log.exception("selective_scan.symbol_error", symbol=sym)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_symbol = {
                pool.submit(
                    _scan_symbol, sym, df, cfg, min_grade, allowed_strategies,
                ): sym
                for sym, df in frames.items()
            }
            for future in as_completed(future_to_symbol):
                sym = future_to_symbol[future]
                try:
                    sig = future.result()
                    if sig is not None:
                        signals.append(sig)
                except Exception:
                    errors += 1
                    log.exception("selective_scan.symbol_error", symbol=sym)

    # Strongest signals first.
    signals.sort(key=lambda s: s.signal_strength, reverse=True)

    elapsed = time.monotonic() - start
    log.info(
        "selective_scan.complete",
        total_symbols=len(symbols),
        signals_found=len(signals),
        errors=errors,
        elapsed_seconds=round(elapsed, 2),
    )
    return signals
