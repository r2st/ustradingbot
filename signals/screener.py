"""
Scan orchestrator for the US Trading Bot.

Iterates over a universe of symbols, tries each strategy in priority
order, and returns the best-qualifying signal per symbol.  This module
is the bridge between raw indicator scoring (:mod:`signals.combined_filter`)
and the trading engine (:mod:`engine`).

Supports parallel scanning via :class:`concurrent.futures.ThreadPoolExecutor`
when the tiered scanning feature is enabled.

Usage::

    from signals.screener import run_full_scan
    from config.universe import ALL_SYMBOLS

    signals = run_full_scan(ALL_SYMBOLS, min_grade="B")
    for sig in signals:
        print(sig.symbol, sig.strategy, sig.grade.value)
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
            # Dedicated detectors (VCP/PEAD/mean-reversion) build their own
            # Signal — attach the TA1 indicator snapshot from the same df
            # here so every winning signal carries it (best-effort).
            if "indicators" not in signal.raw_data:
                try:
                    from signals.indicator_snapshot import (
                        build_indicator_snapshot,
                    )

                    snapshot = build_indicator_snapshot(df)
                    if snapshot is not None:
                        signal.raw_data["indicators"] = snapshot
                except Exception:  # noqa: BLE001
                    log.debug(
                        "scan_symbol.snapshot_failed",
                        symbol=symbol,
                        exc_info=True,
                    )
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
    max_workers: Optional[int] = None,
) -> List[Signal]:
    """Scan the full universe and return qualifying signals.

    For each symbol, strategies are tried in priority order
    (``vcp_breakout -> pead -> momentum -> swing -> mean_reversion``).
    The first strategy that produces a signal with a grade at or above
    *min_grade* wins for that symbol.

    When *max_workers* > 1, symbols are scanned in parallel using a
    :class:`~concurrent.futures.ThreadPoolExecutor`.

    The returned list is sorted by ``signal_strength`` in descending
    order (strongest signals first).

    Args:
        symbols: List of ticker symbols to scan.
        min_grade: Minimum acceptable grade letter.  Defaults to
            ``"B"`` (scores >= 0.65).
        allowed_strategies: Optional strategy whitelist (user trade
            selection); ``None`` runs every strategy.
        max_workers: Thread pool size.  ``None`` or ``1`` disables
            parallelism and uses a simple sequential loop (the legacy
            behaviour).

    Returns:
        List of :class:`Signal` objects, sorted by signal strength
        descending.  May be empty if no symbols qualify.
    """
    start_time = time.monotonic()
    signals: List[Signal] = []
    errors: int = 0

    # Default to settings-based worker count when not specified.
    if max_workers is None:
        settings = get_settings()
        workers = getattr(settings, "TIER1_WORKERS", 1)
        max_workers = workers if workers > 1 else 1

    log.info(
        "run_full_scan.start",
        total_symbols=len(symbols),
        min_grade=min_grade,
        strategies=allowed_strategies or STRATEGY_PRIORITY,
        max_workers=max_workers,
    )

    if max_workers <= 1 or len(symbols) <= 3:
        # Sequential scan (legacy behaviour / small batches).
        for symbol in symbols:
            try:
                signal = _scan_symbol(symbol, min_grade, allowed_strategies)
                if signal is not None:
                    signals.append(signal)
            except Exception:
                errors += 1
                log.exception("run_full_scan.symbol_error", symbol=symbol)
    else:
        # Parallel scan using ThreadPoolExecutor.
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_symbol = {
                pool.submit(_scan_symbol, sym, min_grade, allowed_strategies): sym
                for sym in symbols
            }
            for future in as_completed(future_to_symbol):
                symbol = future_to_symbol[future]
                try:
                    signal = future.result()
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
        max_workers=max_workers,
    )

    return signals


# ---------------------------------------------------------------------------
# Tiered scanning helpers (called by the engine for Tier 2 / Tier 3)
# ---------------------------------------------------------------------------


def run_sector_scan(
    sector: str,
    min_grade: str = "B",
    allowed_strategies: Optional[List[str]] = None,
    max_workers: int = 8,
) -> List[Signal]:
    """Tier 2 scan: scan all active symbols in a GICS sector.

    This is used by the engine's sector rotation loop.
    """
    from config.universe import get_tier2_symbols

    symbols = get_tier2_symbols(sector)
    if not symbols:
        log.debug("run_sector_scan.empty_sector", sector=sector)
        return []
    log.info("run_sector_scan.start", sector=sector, symbols=len(symbols))
    return run_full_scan(
        symbols,
        min_grade=min_grade,
        allowed_strategies=allowed_strategies,
        max_workers=max_workers,
    )


def run_prescreen(
    symbols: List[str],
    price_change_pct: float = 0.03,
    volume_ratio: float = 2.0,
    max_workers: int = 16,
) -> List[str]:
    """Tier 3 pre-screen: lightweight filter for the full universe sweep.

    Returns tickers with a daily price move > *price_change_pct* or daily
    volume > *volume_ratio* times their average.  These qualifying symbols
    are then promoted to Tier 2 for a full scan.
    """
    start = time.monotonic()
    qualifying: List[str] = []

    def _check(symbol: str) -> Optional[str]:
        try:
            df = fetch_ohlcv(symbol, period="5d")
            if df is None or len(df) < 2:
                return None
            latest = df.iloc[-1]
            prev = df.iloc[-2]
            if prev["Close"] <= 0:
                return None
            daily_change = abs(latest["Close"] - prev["Close"]) / prev["Close"]
            if daily_change >= price_change_pct:
                return symbol
            avg_vol = df["Volume"].mean()
            if avg_vol > 0 and latest["Volume"] / avg_vol >= volume_ratio:
                return symbol
        except Exception:  # noqa: BLE001
            pass
        return None

    if max_workers <= 1:
        for sym in symbols:
            result = _check(sym)
            if result:
                qualifying.append(result)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_check, sym): sym for sym in symbols}
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result:
                        qualifying.append(result)
                except Exception:  # noqa: BLE001
                    pass

    elapsed = time.monotonic() - start
    log.info(
        "run_prescreen.complete",
        total=len(symbols),
        qualifying=len(qualifying),
        elapsed_seconds=round(elapsed, 2),
    )
    return qualifying
