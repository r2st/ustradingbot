"""
Hard portfolio-risk limits: sector concentration, correlation, and VaR/CVaR.

These are the pure, side-effect-free building blocks behind the hard gates that
:class:`risk.manager.RiskManager` enforces in ``pre_check`` (P0-1).  Each
function accepts plain data (a positions list, a ``{symbol: returns}`` mapping,
weights) so it unit-tests without any network or filesystem access.

Concepts
--------
* **Sector concentration** — no single sector may exceed
  ``MAX_SECTOR_CONCENTRATION_PCT`` of gross book exposure.  A new entry is
  projected at its *maximum* possible notional so the gate is conservative.
* **Correlation** — a new position whose recent-return correlation with any
  existing holding exceeds ``MAX_POSITION_CORRELATION`` is really the same bet
  twice, so it is rejected.
* **Value at Risk / Conditional VaR** — both the parametric (variance-covariance,
  normal) and the historical (empirical-quantile) estimators are provided, plus
  the expected-shortfall (CVaR) tail average and a portfolio-level aggregate.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from config.universe import get_sector

# Standard-normal inverse-CDF lookup for the common VaR confidence levels, so we
# do not need scipy for a single quantile.  Values are z = Phi^-1(confidence).
_Z_TABLE: Dict[float, float] = {
    0.90: 1.2815515655446004,
    0.95: 1.6448536269514722,
    0.975: 1.9599639845400545,
    0.99: 2.3263478740408408,
    0.995: 2.5758293035489004,
}


def _z_score(confidence: float) -> float:
    """Return the one-tailed z-score for *confidence* (0<c<1).

    Uses an exact table for the common levels and the Acklam rational
    approximation of the inverse normal CDF otherwise.
    """
    c = float(confidence)
    if c in _Z_TABLE:
        return _Z_TABLE[c]
    return _inv_norm_cdf(c)


def _inv_norm_cdf(p: float) -> float:
    """Acklam's inverse normal CDF approximation (abs error < 1.15e-9)."""
    if not 0.0 < p < 1.0:
        raise ValueError("confidence must be in (0, 1)")
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    plow = 0.02425
    phigh = 1 - plow
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
        ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)


# ---------------------------------------------------------------------------
# Sector concentration
# ---------------------------------------------------------------------------


def _position_notional(pos: Dict[str, Any]) -> float:
    try:
        return float(pos.get("entry_price", 0) or 0) * int(
            float(pos.get("quantity", 0) or 0)
        )
    except (ValueError, TypeError):
        return 0.0


def projected_sector_pct(
    positions: Sequence[Dict[str, Any]],
    new_symbol: str,
    new_notional: float,
    capital_base: float = 0.0,
) -> float:
    """Return the projected concentration of *new_symbol*'s sector after opening
    a ``new_notional`` position.

    The concentration is measured against ``max(gross_book, capital_base)`` so
    the gate expresses "no more than X% of *capital* in one sector": early on,
    when little is deployed, a lone position is a small fraction of total
    capital rather than 100% of a one-name book; as the book fills toward full
    deployment the denominator converges on gross book exposure.  Pass
    ``capital_base=0`` (the default) to get the pure book-relative fraction.
    """
    sector = get_sector(new_symbol)
    sector_total = float(new_notional)
    grand_total = float(new_notional)
    for pos in positions:
        notional = _position_notional(pos)
        grand_total += notional
        if get_sector(str(pos.get("symbol", ""))) == sector:
            sector_total += notional
    denom = max(grand_total, float(capital_base))
    if denom <= 0:
        return 0.0
    return sector_total / denom


def sector_cap_check(
    positions: Sequence[Dict[str, Any]],
    new_symbol: str,
    new_notional: float,
    cap_pct: float,
    capital_base: float = 0.0,
) -> Tuple[bool, float]:
    """Return ``(ok, projected_pct)`` for the sector-concentration gate.

    ``ok`` is ``True`` when opening ``new_symbol`` at ``new_notional`` keeps its
    sector at or below ``cap_pct`` of ``max(gross_book, capital_base)``.
    """
    pct = projected_sector_pct(positions, new_symbol, new_notional, capital_base)
    # A tiny tolerance avoids float dust rejecting a position that lands exactly
    # on the cap.
    return pct <= float(cap_pct) + 1e-9, pct


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


def max_correlation_for_symbol(
    new_symbol: str,
    existing_symbols: Sequence[str],
    returns_by_symbol: Dict[str, pd.Series],
    min_overlap: int = 20,
) -> Optional[Tuple[str, float]]:
    """Return the ``(peer, correlation)`` most correlated with *new_symbol*.

    Only peers with at least *min_overlap* overlapping return observations are
    considered.  Returns ``None`` when *new_symbol* has no returns or no peer
    clears the overlap threshold (the caller then fails open — no data, no gate).
    The correlation compared is *signed* Pearson: a strongly negative pair is not
    the same bet, so only positive co-movement trips the gate at the call site.
    """
    sa = returns_by_symbol.get(new_symbol)
    if sa is None or len(sa) < min_overlap:
        return None
    best: Optional[Tuple[str, float]] = None
    for peer in existing_symbols:
        if peer == new_symbol:
            continue
        sb = returns_by_symbol.get(peer)
        if sb is None:
            continue
        joined = pd.concat([sa, sb], axis=1, join="inner").dropna()
        if len(joined) < min_overlap:
            continue
        x = joined.iloc[:, 0].to_numpy()
        y = joined.iloc[:, 1].to_numpy()
        if x.std() == 0 or y.std() == 0:
            continue
        corr = float(np.corrcoef(x, y)[0, 1])
        if np.isnan(corr):
            continue
        if best is None or corr > best[1]:
            best = (peer, corr)
    return best


def correlation_cap_check(
    new_symbol: str,
    existing_symbols: Sequence[str],
    returns_by_symbol: Dict[str, pd.Series],
    max_corr: float,
    min_overlap: int = 20,
) -> Tuple[bool, Optional[Tuple[str, float]]]:
    """Return ``(ok, worst_pair)`` for the correlation gate.

    ``ok`` is ``True`` when no existing holding co-moves with *new_symbol* above
    ``max_corr``.  ``worst_pair`` is the most-correlated ``(peer, corr)`` even
    when it passes, so callers can log/annotate; it is ``None`` when there was
    insufficient data to judge (gate fails open).
    """
    worst = max_correlation_for_symbol(
        new_symbol, existing_symbols, returns_by_symbol, min_overlap
    )
    if worst is None:
        return True, None
    return worst[1] <= float(max_corr), worst


# ---------------------------------------------------------------------------
# Value at Risk / Conditional VaR
# ---------------------------------------------------------------------------


def parametric_var(
    returns: Sequence[float] | pd.Series,
    confidence: float = 0.95,
    horizon_days: int = 1,
) -> float:
    """Variance-covariance (normal) VaR as a *positive* loss fraction.

    ``VaR = -(mu - z*sigma) * sqrt(horizon)`` clipped at zero.  A return series
    with positive drift can produce a negative raw figure at short horizons; the
    reported VaR is floored at 0 (no "negative loss").
    """
    arr = np.asarray(list(returns), dtype=float)
    arr = arr[~np.isnan(arr)]
    if arr.size < 2:
        return 0.0
    mu = float(arr.mean())
    sigma = float(arr.std(ddof=1))
    z = _z_score(confidence)
    scale = math.sqrt(max(1, horizon_days))
    var = -(mu - z * sigma) * scale
    return max(0.0, var)


def historical_var(
    returns: Sequence[float] | pd.Series,
    confidence: float = 0.95,
) -> float:
    """Historical (empirical-quantile) VaR as a positive loss fraction."""
    arr = np.asarray(list(returns), dtype=float)
    arr = arr[~np.isnan(arr)]
    if arr.size < 2:
        return 0.0
    q = float(np.quantile(arr, 1.0 - confidence))
    return max(0.0, -q)


def conditional_var(
    returns: Sequence[float] | pd.Series,
    confidence: float = 0.95,
) -> float:
    """Historical CVaR / expected shortfall as a positive loss fraction.

    The average loss in the worst ``(1-confidence)`` tail of the return
    distribution — a coherent risk measure that captures how bad the tail is,
    not just where it starts.
    """
    arr = np.asarray(list(returns), dtype=float)
    arr = arr[~np.isnan(arr)]
    if arr.size < 2:
        return 0.0
    threshold = float(np.quantile(arr, 1.0 - confidence))
    tail = arr[arr <= threshold]
    if tail.size == 0:
        return max(0.0, -threshold)
    return max(0.0, -float(tail.mean()))


def portfolio_returns(
    returns_by_symbol: Dict[str, pd.Series],
    weights: Dict[str, float],
    min_overlap: int = 20,
) -> Optional[pd.Series]:
    """Weighted portfolio return series from per-symbol returns.

    Weights are normalised to sum to 1 over the symbols that both have returns
    and a positive weight.  Returns ``None`` when fewer than *min_overlap*
    aligned observations exist.
    """
    usable = {
        s: returns_by_symbol[s]
        for s, w in weights.items()
        if w and w > 0 and s in returns_by_symbol
    }
    if not usable:
        return None
    frame = pd.concat(usable.values(), axis=1, join="inner").dropna()
    if len(frame) < min_overlap:
        return None
    total_w = sum(weights[s] for s in usable)
    if total_w <= 0:
        return None
    w_vec = np.array([weights[s] / total_w for s in usable], dtype=float)
    port = frame.to_numpy() @ w_vec
    return pd.Series(port, index=frame.index)


def var_summary(
    returns: Sequence[float] | pd.Series,
    confidence: float = 0.95,
    histogram_bins: int = 24,
) -> Dict[str, Any]:
    """Multi-horizon VaR/CVaR summary + a return-distribution histogram.

    Computes parametric VaR at 1- and 10-day horizons, historical VaR and CVaR
    (all positive loss fractions) at *confidence*, and buckets the return
    series into *histogram_bins* bins for the distribution chart. Everything is
    zeroed with ``observations == 0`` when there is too little history.
    """
    arr = np.asarray(list(returns), dtype=float)
    arr = arr[~np.isnan(arr)]
    conf = float(confidence)
    if arr.size < 2:
        return {
            "var_1d": 0.0, "var_10d": 0.0, "historical_var": 0.0, "cvar": 0.0,
            "confidence": conf, "observations": int(arr.size),
            "histogram": {"bins": [], "counts": []},
        }
    counts, edges = np.histogram(arr, bins=max(4, int(histogram_bins)))
    centers = ((edges[:-1] + edges[1:]) / 2.0)
    return {
        "var_1d": round(parametric_var(arr, conf, 1), 6),
        "var_10d": round(parametric_var(arr, conf, 10), 6),
        "historical_var": round(historical_var(arr, conf), 6),
        "cvar": round(conditional_var(arr, conf), 6),
        "confidence": conf,
        "observations": int(arr.size),
        "histogram": {
            "bins": [round(float(c), 6) for c in centers],
            "counts": [int(c) for c in counts],
        },
    }


def portfolio_var_cvar(
    returns_by_symbol: Dict[str, pd.Series],
    weights: Dict[str, float],
    confidence: float = 0.95,
    horizon_days: int = 1,
    min_overlap: int = 20,
) -> Dict[str, Any]:
    """Return a portfolio VaR/CVaR summary payload.

    Keys: ``parametric_var``, ``historical_var``, ``cvar`` (all positive loss
    fractions), plus ``confidence``, ``horizon_days`` and ``observations``.
    All figures are ``0.0`` with ``observations == 0`` when there is too little
    aligned history to estimate anything.
    """
    port = portfolio_returns(returns_by_symbol, weights, min_overlap)
    if port is None or port.empty:
        return {
            "parametric_var": 0.0,
            "historical_var": 0.0,
            "cvar": 0.0,
            "confidence": float(confidence),
            "horizon_days": int(horizon_days),
            "observations": 0,
        }
    return {
        "parametric_var": round(parametric_var(port, confidence, horizon_days), 6),
        "historical_var": round(historical_var(port, confidence), 6),
        "cvar": round(conditional_var(port, confidence), 6),
        "confidence": float(confidence),
        "horizon_days": int(horizon_days),
        "observations": int(len(port)),
    }
