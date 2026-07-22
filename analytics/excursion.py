"""
Maximum Adverse / Favorable Excursion (MAE / MFE) analytics.

For every open position the engine tracks the two intraday price extremes seen
since entry:

* **MAE** — the *worst* the trade ever looked (the lowest low for a long, the
  highest high for a short).  It measures how much heat a trade took before it
  worked (or died).
* **MFE** — the *best* the trade ever looked (the highest high for a long, the
  lowest low for a short).  It measures how much unrealised profit was on the
  table at the peak.

Both are expressed two ways: as a **percentage** of the entry price and as an
**R-multiple** of the position's initial risk-per-share
(``|entry - original_stop|``), so a $2 stock and a $200 stock are comparable and
excursions line up with the journal's realised ``r_multiple`` column.

The module is deliberately pure — no I/O, no settings, no clock.  The engine
feeds it intraday highs/lows (see :meth:`risk.manager.RiskManager.record_excursions`)
and the dashboard / autotune loop feed it closed-trade records.  Two families of
function:

* **capture** — :func:`update_excursion` ratchets an open position's extremes;
  :func:`position_excursion` derives the pct / R metrics from a position dict.
* **analysis** — :func:`compute_excursion_stats` builds percentile summaries and
  histograms; :func:`analyze_stop_target_efficiency` turns the distributions
  into actionable "stops too tight / targets too conservative" advisories.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_short(direction: Any) -> bool:
    """Whether *direction* denotes a short position."""
    return str(direction or "long").strip().lower() == "short"


def _num(value: Any) -> Optional[float]:
    """Coerce *value* to a finite float, or ``None`` (NaN / junk / empty)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def initial_risk_per_share(
    entry: Any, stop: Any, direction: Any = "long"
) -> Optional[float]:
    """Return the entry-time risk per share, or ``None`` when undefined.

    That is ``entry - stop`` for a long and ``stop - entry`` for a short — always
    the distance price must move *against* the trade to hit the stop.  A stop on
    the wrong side of entry (or a zero distance) yields ``None`` so R-multiples
    degrade gracefully to "unavailable" rather than dividing by zero.
    """
    e = _num(entry)
    s = _num(stop)
    if e is None or s is None:
        return None
    risk = (s - e) if _is_short(direction) else (e - s)
    return risk if risk > 0 else None


# ---------------------------------------------------------------------------
# Capture — ratchet open-position extremes
# ---------------------------------------------------------------------------


def update_excursion(
    pos: Dict[str, Any],
    high: Optional[float] = None,
    low: Optional[float] = None,
) -> Dict[str, Any]:
    """Ratchet *pos*'s ``mae_price`` / ``mfe_price`` with a new intraday bar.

    ``high`` / ``low`` are the session extremes observed this cycle; either may
    be ``None`` (only a last price known) in which case the other is used for
    both.  On the first call the extremes seed from the entry price so a trade
    that has never moved reports a zero excursion rather than a missing one.

    Mutates and returns *pos* (the position dict persisted in
    ``open_positions.json``).  A bar with no usable price is a no-op.
    """
    entry = _num(pos.get("entry_price"))
    if entry is None:
        return pos

    hi = _num(high)
    lo = _num(low)
    if hi is None and lo is None:
        return pos
    # Only one side known -> treat the bar as a single point.
    if hi is None:
        hi = lo
    if lo is None:
        lo = hi
    assert hi is not None and lo is not None  # both-None returned above

    short = _is_short(pos.get("direction"))
    # Seed from entry so the first observation can only widen from break-even.
    cur_mfe = _num(pos.get("mfe_price"))
    cur_mae = _num(pos.get("mae_price"))
    if cur_mfe is None:
        cur_mfe = entry
    if cur_mae is None:
        cur_mae = entry

    if short:
        # Favorable = price falling (use the low); adverse = price rising (high).
        new_mfe = min(cur_mfe, lo)
        new_mae = max(cur_mae, hi)
    else:
        # Favorable = price rising (use the high); adverse = price falling (low).
        new_mfe = max(cur_mfe, hi)
        new_mae = min(cur_mae, lo)

    pos["mfe_price"] = round(new_mfe, 4)
    pos["mae_price"] = round(new_mae, 4)
    return pos


def excursion_metrics(
    entry: Any,
    stop: Any,
    direction: Any,
    mae_price: Any,
    mfe_price: Any,
) -> Dict[str, Optional[float]]:
    """Derive the pct / R excursion metrics from raw prices.  Pure.

    Returns a dict with ``mae_pct``, ``mfe_pct`` (non-negative magnitudes as a
    fraction of entry) and ``mae_r``, ``mfe_r`` (excursion in units of initial
    risk, ``None`` when the entry-time risk is undefined).  Missing extremes fall
    back to the entry price (zero excursion).
    """
    e = _num(entry)
    out: Dict[str, Optional[float]] = {
        "mae_pct": None,
        "mfe_pct": None,
        "mae_r": None,
        "mfe_r": None,
    }
    if e is None or e <= 0:
        return out

    short = _is_short(direction)
    mae_p = _num(mae_price)
    mfe_p = _num(mfe_price)
    if mae_p is None:
        mae_p = e
    if mfe_p is None:
        mfe_p = e

    # Adverse / favorable moves as signed distances in the trade's direction,
    # clamped at 0 so a never-moved trade reports 0 rather than a tiny negative.
    if short:
        adverse = max(0.0, mae_p - e)
        favorable = max(0.0, e - mfe_p)
    else:
        adverse = max(0.0, e - mae_p)
        favorable = max(0.0, mfe_p - e)

    out["mae_pct"] = round(adverse / e, 6)
    out["mfe_pct"] = round(favorable / e, 6)

    risk = initial_risk_per_share(e, stop, direction)
    if risk is not None:
        out["mae_r"] = round(adverse / risk, 4)
        out["mfe_r"] = round(favorable / risk, 4)
    return out


def position_excursion(pos: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Compute excursion metrics for an open/closed position dict.

    Uses ``original_stop_loss`` (the entry-time stop) for the R denominator when
    present so a ratcheted live stop doesn't shrink the measured risk; falls back
    to ``stop_price``.
    """
    stop = pos.get("original_stop_loss")
    if stop is None:
        stop = pos.get("stop_price")
    return excursion_metrics(
        pos.get("entry_price"),
        stop,
        pos.get("direction"),
        pos.get("mae_price"),
        pos.get("mfe_price"),
    )


# ---------------------------------------------------------------------------
# Analysis — distributions
# ---------------------------------------------------------------------------


def percentile_summary(values: Sequence[float]) -> Dict[str, float]:
    """Return count / mean / min / max and p10-p90 percentiles of *values*.

    Non-finite values are dropped.  An empty input returns all-zero fields with
    ``count = 0`` so callers can render "no data" without special-casing.
    """
    clean = sorted(v for v in (_num(x) for x in values) if v is not None)
    n = len(clean)
    if n == 0:
        return {
            "count": 0, "mean": 0.0, "min": 0.0, "max": 0.0,
            "p10": 0.0, "p25": 0.0, "p50": 0.0, "p75": 0.0, "p90": 0.0,
        }

    def _pct(p: float) -> float:
        # Linear-interpolation percentile (matches numpy's default "linear").
        if n == 1:
            return clean[0]
        rank = p / 100.0 * (n - 1)
        lo = int(math.floor(rank))
        hi = int(math.ceil(rank))
        if lo == hi:
            return clean[lo]
        frac = rank - lo
        return clean[lo] * (1 - frac) + clean[hi] * frac

    return {
        "count": n,
        "mean": round(sum(clean) / n, 4),
        "min": round(clean[0], 4),
        "max": round(clean[-1], 4),
        "p10": round(_pct(10), 4),
        "p25": round(_pct(25), 4),
        "p50": round(_pct(50), 4),
        "p75": round(_pct(75), 4),
        "p90": round(_pct(90), 4),
    }


def histogram(
    values: Sequence[float],
    bin_width: float,
    max_edge: Optional[float] = None,
) -> Dict[str, Any]:
    """Bucket *values* into fixed-width bins starting at 0.

    Returns ``{"bins": [left_edges], "counts": [n]}``.  Values at or beyond
    ``max_edge`` (default: the largest value, rounded up to a bin) land in the
    final bin so a long tail never explodes the bin count.  Excursion magnitudes
    are non-negative, so bins start at 0.
    """
    clean = [v for v in (_num(x) for x in values) if v is not None and v >= 0]
    if not clean or bin_width <= 0:
        return {"bins": [], "counts": []}

    top = max(clean) if max_edge is None else max_edge
    n_bins = max(1, int(math.ceil((top + 1e-9) / bin_width)))
    n_bins = min(n_bins, 40)  # guard against a pathological bin_width
    counts = [0] * n_bins
    for v in clean:
        idx = int(v / bin_width)
        if idx >= n_bins:
            idx = n_bins - 1
        counts[idx] += 1
    bins = [round(i * bin_width, 4) for i in range(n_bins)]
    return {"bins": bins, "counts": counts}


def _records_to_series(
    records: Sequence[Dict[str, Any]], field: str
) -> List[float]:
    """Pull a numeric *field* out of trade records, dropping missing values."""
    out: List[float] = []
    for r in records:
        v = _num(r.get(field))
        if v is not None:
            out.append(v)
    return out


def compute_excursion_stats(
    records: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build MAE/MFE distribution analytics from closed-trade *records*.

    Each record should carry ``mae_pct``, ``mfe_pct``, ``mae_r``, ``mfe_r`` (as
    written to the journal at exit) and, when available, ``pnl_net`` /
    ``r_multiple`` for the winner/loser split.  Returns percentile summaries and
    histograms for both excursions plus a small winner-vs-loser breakdown.
    """
    records = [r for r in records if isinstance(r, dict)]
    mae_pct = _records_to_series(records, "mae_pct")
    mfe_pct = _records_to_series(records, "mfe_pct")
    mae_r = _records_to_series(records, "mae_r")
    mfe_r = _records_to_series(records, "mfe_r")

    winners = [r for r in records if (_num(r.get("pnl_net")) or 0.0) > 0]
    losers = [r for r in records if (_num(r.get("pnl_net")) or 0.0) < 0]

    return {
        "trades": len(records),
        "mae_pct": {
            "summary": percentile_summary(mae_pct),
            "histogram": histogram(mae_pct, 0.01),  # 1%-wide bins
        },
        "mfe_pct": {
            "summary": percentile_summary(mfe_pct),
            "histogram": histogram(mfe_pct, 0.01),
        },
        "mae_r": {
            "summary": percentile_summary(mae_r),
            "histogram": histogram(mae_r, 0.25),  # quarter-R bins
        },
        "mfe_r": {
            "summary": percentile_summary(mfe_r),
            "histogram": histogram(mfe_r, 0.25),
        },
        "winners": {
            "count": len(winners),
            "mae_r": percentile_summary(_records_to_series(winners, "mae_r")),
            "mfe_r": percentile_summary(_records_to_series(winners, "mfe_r")),
        },
        "losers": {
            "count": len(losers),
            "mae_r": percentile_summary(_records_to_series(losers, "mae_r")),
            "mfe_r": percentile_summary(_records_to_series(losers, "mfe_r")),
        },
    }


# ---------------------------------------------------------------------------
# Analysis — stop / target efficiency advisories
# ---------------------------------------------------------------------------

# Defaults for the efficiency heuristics.  Tunable so tests are deterministic
# and operators can retune from settings without editing the logic.
DEFAULT_MIN_SAMPLE = 8          # min trades in a bucket before we advise
DEFAULT_NEAR_STOP_R = 0.85      # MAE within this R of the stop == "near stop"
DEFAULT_TIGHT_FRACTION = 0.35   # ...on this share of winners == too tight
DEFAULT_GIVEBACK_R = 1.0        # a stopped loser that had reached this MFE R
DEFAULT_GIVEBACK_FRACTION = 0.30
DEFAULT_LEFTOVER_R = 0.75       # median unrealised R left on winners
DEFAULT_LOW_CAPTURE = 0.5       # median realised/available move ratio


def _fraction(predicate_hits: int, total: int) -> float:
    return (predicate_hits / total) if total else 0.0


def analyze_stop_target_efficiency(
    records: Sequence[Dict[str, Any]],
    *,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    near_stop_r: float = DEFAULT_NEAR_STOP_R,
    tight_fraction: float = DEFAULT_TIGHT_FRACTION,
    giveback_r: float = DEFAULT_GIVEBACK_R,
    giveback_fraction: float = DEFAULT_GIVEBACK_FRACTION,
    leftover_r: float = DEFAULT_LEFTOVER_R,
) -> Dict[str, Any]:
    """Turn MAE/MFE distributions into stop / target advisories.

    Produces a list of advisory dicts (``id``, ``severity``, ``title``,
    ``message`` + supporting metrics).  Three signals:

    * **stops_too_tight** — a large share of *eventual winners* dipped to within
      ``near_stop_r`` of their stop first: the stop is nearly ejecting trades
      that go on to work, so it (or entry timing) is too tight.
    * **stops_giveback** — a large share of *stopped-out losers* had already
      reached ``giveback_r`` of favourable excursion before reversing into the
      stop: profit is being handed back; consider a breakeven/partial-take.
    * **targets_too_conservative** — winners leave a median of ``leftover_r`` or
      more unrealised R on the table beyond their exit: targets cap the runners
      too early.

    Buckets with fewer than ``min_sample`` trades are skipped (reported as
    ``insufficient_data`` advisories) so a handful of trades can't trigger a
    threshold change.
    """
    records = [r for r in records if isinstance(r, dict)]
    winners = [r for r in records if (_num(r.get("pnl_net")) or 0.0) > 0]
    losers = [r for r in records if (_num(r.get("pnl_net")) or 0.0) < 0]

    advisories: List[Dict[str, Any]] = []

    # -- stops too tight (winners that nearly hit their stop first) -----------
    winner_mae = _records_to_series(winners, "mae_r")
    if len(winner_mae) >= min_sample:
        near = sum(1 for v in winner_mae if v >= near_stop_r)
        frac = _fraction(near, len(winner_mae))
        if frac >= tight_fraction:
            advisories.append({
                "id": "stops_too_tight",
                "severity": "warn",
                "title": "Stops may be too tight",
                "message": (
                    f"{frac * 100:.0f}% of winning trades first fell to within "
                    f"{near_stop_r:.2f}R of their stop. A slightly wider stop "
                    f"(or later entry) would have held these winners with more "
                    f"margin."
                ),
                "metric": round(frac, 4),
                "threshold": tight_fraction,
                "sample": len(winner_mae),
            })
    else:
        advisories.append(_insufficient("stops_too_tight", len(winner_mae), min_sample))

    # -- stops giving back gains (stopped losers that had run up first) -------
    stopped = [r for r in losers if _is_stop_exit(r)]
    give_series = _records_to_series(stopped, "mfe_r")
    if len(give_series) >= min_sample:
        gb = sum(1 for v in give_series if v >= giveback_r)
        frac = _fraction(gb, len(give_series))
        if frac >= giveback_fraction:
            advisories.append({
                "id": "stops_giveback",
                "severity": "warn",
                "title": "Winners reversing into stops",
                "message": (
                    f"{frac * 100:.0f}% of stopped-out losers had already reached "
                    f"{giveback_r:.1f}R of profit before reversing. A breakeven "
                    f"stop or partial take would lock in some of that move."
                ),
                "metric": round(frac, 4),
                "threshold": giveback_fraction,
                "sample": len(give_series),
            })

    # -- targets too conservative (winners with lots of R left on the table) --
    leftover = [
        (mfe - r)
        for r, mfe in (
            (_num(rec.get("r_multiple")), _num(rec.get("mfe_r")))
            for rec in winners
        )
        if r is not None and mfe is not None
    ]
    if len(leftover) >= min_sample:
        median_left = percentile_summary(leftover)["p50"]
        if median_left >= leftover_r:
            advisories.append({
                "id": "targets_too_conservative",
                "severity": "warn",
                "title": "Targets may be too conservative",
                "message": (
                    f"Winning trades left a median of {median_left:.2f}R "
                    f"unrealised beyond their exit (peak MFE vs realised R). "
                    f"Wider targets or a trailing runner would capture more."
                ),
                "metric": round(median_left, 4),
                "threshold": leftover_r,
                "sample": len(leftover),
            })
    else:
        advisories.append(
            _insufficient("targets_too_conservative", len(leftover), min_sample)
        )

    return {
        "advisories": advisories,
        "winners": len(winners),
        "losers": len(losers),
        "trades": len(records),
    }


def excursion_report(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Full excursion payload for the dashboard: distributions + advisories.

    Combines :func:`compute_excursion_stats` and
    :func:`analyze_stop_target_efficiency` into the single object the
    ``/api/excursion`` endpoint returns.
    """
    stats = compute_excursion_stats(records)
    efficiency = analyze_stop_target_efficiency(records)
    return {
        "trades": stats["trades"],
        "distributions": {
            "mae_pct": stats["mae_pct"],
            "mfe_pct": stats["mfe_pct"],
            "mae_r": stats["mae_r"],
            "mfe_r": stats["mfe_r"],
        },
        "winners": stats["winners"],
        "losers": stats["losers"],
        "efficiency": efficiency,
    }


def records_from_dataframe(df: Any) -> List[Dict[str, Any]]:
    """Extract excursion-analysis records from a completed-trades DataFrame.

    Keeps only the columns the analytics need and returns a list of plain dicts
    (the pure functions above operate on dicts, never pandas).  A row missing
    both excursion magnitudes is dropped so pre-excursion history doesn't dilute
    the distributions with zeros.
    """
    if df is None or getattr(df, "empty", True):
        return []
    cols = [
        c
        for c in ("mae_pct", "mfe_pct", "mae_r", "mfe_r", "r_multiple",
                  "pnl_net", "exit_reason", "strategy", "symbol")
        if c in df.columns
    ]
    if not cols:
        return []
    records: List[Dict[str, Any]] = []
    for row in df[cols].to_dict(orient="records"):
        if _num(row.get("mae_pct")) is None and _num(row.get("mfe_pct")) is None:
            continue
        records.append(row)
    return records


def _is_stop_exit(record: Dict[str, Any]) -> bool:
    """Heuristic: whether a trade record was closed by a stop-loss."""
    reason = str(record.get("exit_reason", "")).upper()
    return "STOP" in reason


def _insufficient(advisory_id: str, have: int, need: int) -> Dict[str, Any]:
    return {
        "id": advisory_id,
        "severity": "info",
        "title": "Not enough trades yet",
        "message": f"Need {need} trades to assess; have {have}.",
        "metric": None,
        "threshold": None,
        "sample": have,
    }
