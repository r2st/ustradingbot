"""
F2 — Similar-Setup Guard.

Before the engine places a new entry it asks: *"have I traded a setup like this
before, and how did it go?"*  This module answers that from the bot's own
ledger (``trades.csv``).  It filters completed trades down to those that look
like the incoming signal — same strategy and direction, same grade, RSI and
volume ratio within a tolerance band, inside a lookback window — and computes
the historical win rate and average R-multiple.

The result drives a decision:

* **proceed** — not enough history to judge, or the record is acceptable.
* **demote**  — the win rate is poor; take the trade only if it is grade A.
* **block**   — the win rate is very poor over a solid sample; skip entirely.

The module is pure and local: it never calls an API and never mutates state.
It is fail-open by construction — any read error yields a ``proceed`` result
(see :func:`find_similar_setups`), so a corrupt ledger can never halt trading.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import structlog

from analytics.performance import load_completed_trades
from config.settings import EASTERN

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

PROCEED = "proceed"
DEMOTE = "demote"
BLOCK = "block"


@dataclass
class SimilarSetupResult:
    """Outcome of a similar-setup lookup.

    Attributes:
        action: ``"proceed"``, ``"demote"`` (grade-A only), or ``"block"``.
        match_count: Number of historical trades matching the setup.
        win_rate: Fraction of matches with ``pnl_net > 0`` (0.0-1.0).
        avg_realized_r: Mean ``r_multiple`` across the matches.
        avg_days_held: Mean holding period in days across the matches.
        reason: Human-readable justification.
    """

    action: str = PROCEED
    match_count: int = 0
    win_rate: float = 0.0
    avg_realized_r: float = 0.0
    avg_days_held: float = 0.0
    reason: str = "similar-setup guard not run"

    @property
    def blocks(self) -> bool:
        """``True`` when the setup should be skipped outright."""
        return self.action == BLOCK

    @property
    def demotes(self) -> bool:
        """``True`` when the setup should be restricted to grade A."""
        return self.action == DEMOTE


def _within(value: float, target: float, tol: float) -> bool:
    """Whether *value* is within ``±tol`` of *target*.

    NaN-, blank-, and garbage-safe: any value that can't be read as a number
    (empty cell, ``None``, non-numeric string) is treated as a non-match.
    """
    try:
        if pd.isna(value) or pd.isna(target):
            return False
        return abs(float(value) - float(target)) <= tol
    except (TypeError, ValueError):
        return False


def match_similar_trades(
    trades: pd.DataFrame,
    *,
    strategy: str,
    direction: str,
    grade: str,
    rsi_value: float,
    volume_ratio: float,
    settings,
    now: Optional[datetime] = None,
) -> pd.DataFrame:
    """Return the subset of *trades* similar to the described setup.

    Pure filter over an already-loaded completed-trades frame.  A trade is
    "similar" when it shares the strategy, direction and grade, its RSI and
    volume ratio fall inside the configured tolerance bands, and it closed
    within ``SIMILAR_SETUP_LOOKBACK_DAYS``.  Returns an empty frame when the
    input lacks the columns needed to judge similarity.
    """
    if trades is None or trades.empty:
        return trades if isinstance(trades, pd.DataFrame) else pd.DataFrame()

    required = {"strategy", "grade", "rsi_value", "volume_ratio", "pnl_net"}
    if not required.issubset(trades.columns):
        return pd.DataFrame()

    df = trades.copy()

    # Strategy + grade (case-insensitive string compare).
    df = df[df["strategy"].astype(str).str.lower() == str(strategy).lower()]
    df = df[df["grade"].astype(str).str.upper() == str(grade).upper()]

    # Direction: the journal defaults blanks to long; treat missing as long.
    if "direction" in df.columns:
        dir_col = df["direction"].astype(str).str.lower().replace("", "long")
        df = df[dir_col == str(direction).lower()]

    if df.empty:
        return df

    # Lookback window on exit_time.
    if "exit_time" in df.columns:
        now = now or datetime.now(tz=EASTERN)
        cutoff = now - timedelta(days=int(settings.SIMILAR_SETUP_LOOKBACK_DAYS))
        exit_dt = pd.to_datetime(df["exit_time"], errors="coerce", utc=True)
        cutoff_utc = pd.Timestamp(cutoff).tz_convert("UTC")
        df = df[exit_dt >= cutoff_utc]

    if df.empty:
        return df

    # RSI + volume tolerance bands.
    rsi_tol = float(settings.SIMILAR_SETUP_RSI_TOLERANCE)
    vol_tol = float(settings.SIMILAR_SETUP_VOL_TOLERANCE)
    mask = df.apply(
        lambda r: _within(r["rsi_value"], rsi_value, rsi_tol)
        and _within(r["volume_ratio"], volume_ratio, vol_tol),
        axis=1,
    )
    return df[mask]


def evaluate(matches: pd.DataFrame, settings) -> SimilarSetupResult:
    """Turn a set of matching trades into a :class:`SimilarSetupResult`.

    With fewer than ``SIMILAR_SETUP_MIN_MATCHES`` matches the sample is too
    small to act on and the result is ``proceed``.  Otherwise the win rate is
    compared against the block/demote thresholds.
    """
    count = int(len(matches))
    min_matches = int(settings.SIMILAR_SETUP_MIN_MATCHES)

    if count == 0:
        return SimilarSetupResult(
            action=PROCEED, reason="no similar historical setups"
        )

    pnl = pd.to_numeric(matches["pnl_net"], errors="coerce").dropna()
    wins = int((pnl > 0).sum())
    win_rate = wins / len(pnl) if len(pnl) else 0.0

    avg_r = 0.0
    if "r_multiple" in matches.columns:
        r = pd.to_numeric(matches["r_multiple"], errors="coerce").dropna()
        avg_r = float(r.mean()) if len(r) else 0.0

    avg_days = 0.0
    if "hold_duration_hours" in matches.columns:
        h = pd.to_numeric(matches["hold_duration_hours"], errors="coerce").dropna()
        avg_days = float(h.mean() / 24.0) if len(h) else 0.0

    base = SimilarSetupResult(
        match_count=count,
        win_rate=round(win_rate, 4),
        avg_realized_r=round(avg_r, 4),
        avg_days_held=round(avg_days, 2),
    )

    if count < min_matches:
        base.action = PROCEED
        base.reason = (
            f"only {count} similar trades (< {min_matches}); insufficient "
            "sample, proceeding"
        )
        return base

    if win_rate < float(settings.SIMILAR_SETUP_BLOCK_WIN_RATE):
        base.action = BLOCK
        base.reason = (
            f"similar setups won {win_rate:.0%} over {count} trades "
            f"(< {settings.SIMILAR_SETUP_BLOCK_WIN_RATE:.0%}) — skipping"
        )
        return base

    if win_rate < float(settings.SIMILAR_SETUP_MIN_WIN_RATE):
        base.action = DEMOTE
        base.reason = (
            f"similar setups won {win_rate:.0%} over {count} trades "
            f"(< {settings.SIMILAR_SETUP_MIN_WIN_RATE:.0%}) — grade-A only"
        )
        return base

    base.action = PROCEED
    base.reason = (
        f"similar setups won {win_rate:.0%} over {count} trades — acceptable"
    )
    return base


def find_similar_setups(
    signal,
    csv_path: str | Path,
    settings,
    now: Optional[datetime] = None,
) -> SimilarSetupResult:
    """Load the ledger and evaluate *signal* against similar past trades.

    This is the single entry point used by the engine.  Fail-open: any error
    reading or parsing the ledger returns a ``proceed`` result so the guard can
    never take down the entry pipeline.

    Args:
        signal: The candidate :class:`~signals.signal_types.Signal`.
        csv_path: Path to ``trades.csv``.
        settings: Application settings (thresholds + tolerances).
        now: Reference time for the lookback window (defaults to now, ET).

    Returns:
        A :class:`SimilarSetupResult`.
    """
    try:
        trades = load_completed_trades(csv_path)
        matches = match_similar_trades(
            trades,
            strategy=signal.strategy,
            direction=getattr(signal, "direction", "long"),
            grade=signal.grade.value,
            rsi_value=float(signal.rsi_value),
            volume_ratio=float(signal.volume_ratio),
            settings=settings,
            now=now,
        )
        return evaluate(matches, settings)
    except Exception as exc:  # noqa: BLE001 -- fail open, never block a trade
        log.warning("setup_similarity.error", error=str(exc))
        return SimilarSetupResult(
            action=PROCEED, reason=f"similar-setup guard errored ({exc}) — open"
        )
