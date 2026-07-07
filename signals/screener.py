"""
Scan orchestrator for the US Trading Bot.

Iterates over a universe of symbols, tries each strategy in priority
order, and returns the best-qualifying signal per symbol.  This module
is the bridge between raw indicator scoring (:mod:`signals.combined_filter`)
and the trading engine (:mod:`engine`).

Usage::

    from signals.screener import run_full_scan
    from config.universe import ALL_SYMBOLS

    signals = run_full_scan(ALL_SYMBOLS, min_grade="B")
    for sig in signals:
        print(sig.symbol, sig.strategy, sig.grade.value)
"""

from __future__ import annotations

import time
from typing import List, Optional

import structlog

from config.settings import get_settings
from data.fetcher import fetch_ohlcv
from signals.combined_filter import score_symbol
from signals.multi_timeframe import weekly_confirms
from signals.mean_reversion_signal import detect as detect_mean_reversion
from signals.pead_signal import detect as detect_pead
from signals.signal_types import Grade, Signal
from signals.vcp_signal import detect as detect_vcp

log = structlog.get_logger(__name__)

# Dedicated strategy detectors.  Strategies not listed here (momentum, swing)
# fall through to the generic weighted scoring engine (``score_symbol``).
_DEDICATED_DETECTORS = {
    "vcp_breakout": detect_vcp,
    "pead": detect_pead,
    "mean_reversion": detect_mean_reversion,
}

# Strategy priority order.  The first strategy to produce a qualifying
# signal wins for that symbol — later strategies are not attempted.
STRATEGY_PRIORITY: List[str] = [
    "vcp_breakout",
    "pead",
    "momentum",
    "swing",
    "mean_reversion",
]

# Map grade letters to an ordinal for comparison.
_GRADE_RANK = {"A": 0, "B": 1, "C": 2, "F": 3}


def _grade_meets_minimum(grade: Grade, min_grade: str) -> bool:
    """Return ``True`` if *grade* is at least as good as *min_grade*.

    Args:
        grade: The signal's quality grade.
        min_grade: Minimum acceptable grade letter (``"A"``, ``"B"``,
            ``"C"``, or ``"F"``).

    Returns:
        ``True`` when the grade's rank is equal to or better than
        *min_grade*'s rank.
    """
    return _GRADE_RANK.get(grade.value, 99) <= _GRADE_RANK.get(min_grade.upper(), 99)


def _scan_symbol(
    symbol: str,
    min_grade: str,
    allowed_strategies: Optional[List[str]] = None,
) -> Optional[Signal]:
    """Try each strategy for a single symbol and return the first qualifying signal.

    Strategies are attempted in :data:`STRATEGY_PRIORITY` order.  The
    first signal whose grade meets *min_grade* is returned immediately;
    remaining strategies are skipped.

    Args:
        symbol: Ticker symbol to scan.
        min_grade: Minimum acceptable grade letter.
        allowed_strategies: Optional whitelist of strategy names to try
            (``None`` means all strategies).

    Returns:
        The winning :class:`Signal`, or ``None`` if no strategy qualifies.
    """
    settings = get_settings()
    strategies = [
        s for s in STRATEGY_PRIORITY
        if allowed_strategies is None or s in allowed_strategies
    ]

    # Fetch OHLCV data once — shared across all strategy attempts.  The window
    # must be long enough to clear settings.MIN_OHLCV_ROWS (EMA-200 needs 200
    # bars); the default "6mo" (~123 bars) is too short and rejects everything.
    df = fetch_ohlcv(symbol, period=settings.OHLCV_FETCH_PERIOD)
    if df is None:
        log.debug("scan_symbol.no_data", symbol=symbol)
        return None

    if len(df) < settings.MIN_OHLCV_ROWS:
        log.debug(
            "scan_symbol.insufficient_rows",
            symbol=symbol,
            rows=len(df),
            required=settings.MIN_OHLCV_ROWS,
        )
        return None

    for strategy in strategies:
        # VCP, PEAD, and mean-reversion use dedicated pattern detectors.
        # Momentum and swing use the generic weighted scoring engine.
        detector = _DEDICATED_DETECTORS.get(strategy)
        if detector is not None:
            signal = detector(symbol, df)
        else:
            signal = score_symbol(symbol, strategy, df)

        if signal is None:
            continue

        if _grade_meets_minimum(signal.grade, min_grade):
            # Multi-timeframe confirmation: only take the daily signal if it
            # aligns with the weekly trend (no-op when the feature is off).
            if not weekly_confirms(df, settings, signal.direction):
                log.debug(
                    "scan_symbol.weekly_veto",
                    symbol=symbol,
                    strategy=strategy,
                    grade=signal.grade.value,
                )
                continue
            log.debug(
                "scan_symbol.hit",
                symbol=symbol,
                strategy=strategy,
                grade=signal.grade.value,
                signal_strength=signal.signal_strength,
            )
            return signal

        log.debug(
            "scan_symbol.grade_below_minimum",
            symbol=symbol,
            strategy=strategy,
            grade=signal.grade.value,
            min_grade=min_grade,
        )

    return None


def run_full_scan(
    symbols: List[str],
    min_grade: str = "B",
    allowed_strategies: Optional[List[str]] = None,
) -> List[Signal]:
    """Scan the full universe and return qualifying signals.

    For each symbol, strategies are tried in priority order
    (``vcp_breakout -> pead -> momentum -> swing -> mean_reversion``).
    The first strategy that produces a signal with a grade at or above
    *min_grade* wins for that symbol.

    The returned list is sorted by ``signal_strength`` in descending
    order (strongest signals first).

    Args:
        symbols: List of ticker symbols to scan.
        min_grade: Minimum acceptable grade letter.  Defaults to
            ``"B"`` (scores >= 0.65).
        allowed_strategies: Optional strategy whitelist (user trade
            selection); ``None`` runs every strategy.

    Returns:
        List of :class:`Signal` objects, sorted by signal strength
        descending.  May be empty if no symbols qualify.
    """
    start_time = time.monotonic()
    signals: List[Signal] = []
    errors: int = 0

    log.info(
        "run_full_scan.start",
        total_symbols=len(symbols),
        min_grade=min_grade,
        strategies=allowed_strategies or STRATEGY_PRIORITY,
    )

    for symbol in symbols:
        try:
            signal = _scan_symbol(symbol, min_grade, allowed_strategies)
            if signal is not None:
                signals.append(signal)
        except Exception:
            errors += 1
            log.exception("run_full_scan.symbol_error", symbol=symbol)

    # Sort strongest signals first.
    signals.sort(key=lambda s: s.signal_strength, reverse=True)

    elapsed = time.monotonic() - start_time
    log.info(
        "run_full_scan.complete",
        total_symbols=len(symbols),
        signals_found=len(signals),
        errors=errors,
        elapsed_seconds=round(elapsed, 2),
    )

    return signals
