"""
Strategy auto-tuning (feature 12).

Nudges the grade thresholds up or down based on the recent hit-rate of the last
``AUTOTUNE_LOOKBACK_TRADES`` closed trades:

* a **cold** streak (win rate < 0.4) **raises** the thresholds — fewer, higher
  quality entries while the edge is off;
* a **hot** streak (win rate > 0.6) **relaxes** them slightly;
* in between, nothing changes.

The adjustment magnitude scales with the distance of the win rate from 0.5 and
is clamped to ``±AUTOTUNE_MAX_GRADE_ADJUST``.  The same delta is applied to the
A/B/C thresholds, which are kept ordered and inside ``[0.05, 0.95]``.

:func:`tune` is pure over a trades DataFrame; :func:`tune_from_journal` wires it
to the live journal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

import pandas as pd

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_COLD = 0.4
_HOT = 0.6
_FLOOR = 0.05
_CEIL = 0.95


@dataclass
class TuneResult:
    """Outcome of an auto-tune pass."""

    enabled: bool
    applied: bool
    trades_considered: int
    win_rate: float
    delta: float
    thresholds: Dict[str, float] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "enabled": self.enabled,
            "applied": self.applied,
            "trades_considered": self.trades_considered,
            "win_rate": round(self.win_rate, 4),
            "delta": round(self.delta, 4),
            "thresholds": {k: round(v, 4) for k, v in self.thresholds.items()},
            "reason": self.reason,
        }


def _current_thresholds(settings) -> Dict[str, float]:
    return {
        "A": float(getattr(settings, "GRADE_A_THRESHOLD", 0.78)),
        "B": float(getattr(settings, "GRADE_B_THRESHOLD", 0.65)),
        "C": float(getattr(settings, "GRADE_C_THRESHOLD", 0.38)),
    }


def compute_adjustment(win_rate: float, settings) -> float:
    """Return the clamped threshold delta for *win_rate*.

    Positive (raise thresholds) on a cold streak, negative (relax) on a hot
    streak, ``0.0`` in the neutral band.  ``win_rate == 0.5`` -> ``0.0``.
    """
    max_adjust = float(getattr(settings, "AUTOTUNE_MAX_GRADE_ADJUST", 0.08))
    if win_rate < _COLD:
        # 0 at the cold boundary, full magnitude at win_rate == 0.
        frac = (_COLD - win_rate) / _COLD
        return round(min(max_adjust, max_adjust * frac), 4)
    if win_rate > _HOT:
        frac = (win_rate - _HOT) / (1.0 - _HOT)
        return round(-min(max_adjust, max_adjust * frac), 4)
    return 0.0


def _apply_delta(thresholds: Dict[str, float], delta: float) -> Dict[str, float]:
    raised = {k: max(_FLOOR, min(_CEIL, v + delta)) for k, v in thresholds.items()}
    # Preserve strict ordering A > B > C after clamping.
    a = raised["A"]
    b = min(raised["B"], a - 0.01)
    c = min(raised["C"], b - 0.01)
    return {
        "A": round(a, 4),
        "B": round(max(_FLOOR, b), 4),
        "C": round(max(_FLOOR, c), 4),
    }


def tune(trades: pd.DataFrame, settings) -> TuneResult:
    """Compute tuned thresholds from a completed-trades DataFrame.  Pure."""
    current = _current_thresholds(settings)
    if not getattr(settings, "AUTOTUNE_ENABLED", False):
        return TuneResult(False, False, 0, 0.0, 0.0, current, "disabled")

    min_trades = int(getattr(settings, "AUTOTUNE_MIN_TRADES", 15))
    lookback = int(getattr(settings, "AUTOTUNE_LOOKBACK_TRADES", 30))

    if trades is None or trades.empty or "pnl_net" not in trades.columns:
        return TuneResult(True, False, 0, 0.0, 0.0, current, "no completed trades")

    df = trades
    if "exit_time" in df.columns:
        df = df.sort_values("exit_time")
    pnl = pd.to_numeric(df["pnl_net"], errors="coerce").dropna()
    if len(pnl) < min_trades:
        return TuneResult(
            True, False, int(len(pnl)), 0.0, 0.0, current,
            f"insufficient trades ({len(pnl)} < {min_trades})",
        )

    window = pnl.tail(lookback)
    win_rate = float((window > 0).mean())
    delta = compute_adjustment(win_rate, settings)
    new_thresholds = _apply_delta(current, delta) if delta else dict(current)
    applied = delta != 0.0
    if not applied:
        reason = f"neutral win rate {win_rate:.2f} — no change"
    elif delta > 0:
        reason = f"cold streak (win rate {win_rate:.2f}) — raising thresholds by {delta:+.3f}"
    else:
        reason = f"hot streak (win rate {win_rate:.2f}) — relaxing thresholds by {delta:+.3f}"
    return TuneResult(True, applied, int(len(window)), win_rate, delta, new_thresholds, reason)


def tune_from_journal(settings) -> TuneResult:
    """Auto-tune from the live trade journal.  Never raises."""
    current = _current_thresholds(settings)
    try:
        from pathlib import Path

        from analytics.performance import load_completed_trades

        df = load_completed_trades(Path(settings.DATA_DIR) / "trades.csv")
        return tune(df, settings)
    except Exception as exc:  # noqa: BLE001
        log.warning("autotune.journal_failed", error=str(exc))
        return TuneResult(
            bool(getattr(settings, "AUTOTUNE_ENABLED", False)),
            False, 0, 0.0, 0.0, current, "journal load failed",
        )
