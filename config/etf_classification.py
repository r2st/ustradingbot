"""
ETF leverage / inverse sub-classification — critical for risk sizing.

The base :mod:`config.etf_universe` answers *"is this an ETF?"*.  This module
answers the follow-up the risk manager actually cares about: *"what kind of ETF
— a plain diversified basket, or a geared product that moves 2×/3× the index (or
inversely)?"*  Leveraged and inverse ETFs (TQQQ, SQQQ, UVXY, SPXS, …) decay,
compound daily, and move far more than the underlying, so handing them the same
1.3× risk budget a broad-market fund gets is dangerous.

Everything here is **pure and static** — no network, no settings, no DB — so it
is safe to call from the sizing hot path in :mod:`risk.manager`.  Detection uses
two fail-open layers:

1. A curated static map of well-known geared tickers (authoritative).
2. Naming-convention heuristics over a fund's name (``"UltraPro"``, ``"3X"``,
   ``"Bear"``, ``"Inverse"``, …) — used when a live ``yfinance`` ``info`` dict is
   supplied, so newly-listed products classify without a code change.

Unknown / plain ETFs classify as :data:`REGULAR`, which maps to the existing
ETF risk parameters, so behaviour is never worse than before.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

# ── Leverage categories ─────────────────────────────────────────────────────
# String constants (not an Enum) so they serialise straight into card JSON and
# compare cheaply in the hot path.
REGULAR: str = "regular"
LEVERAGED_2X: str = "leveraged_2x"
LEVERAGED_3X: str = "leveraged_3x"
INVERSE: str = "inverse"
LEVERAGED_INVERSE: str = "leveraged_inverse"

ALL_CATEGORIES: Tuple[str, ...] = (
    REGULAR,
    LEVERAGED_2X,
    LEVERAGED_3X,
    INVERSE,
    LEVERAGED_INVERSE,
)

#: Categories that warrant a shrunk risk budget / UI warning.
GEARED_CATEGORIES: frozenset[str] = frozenset(
    {LEVERAGED_2X, LEVERAGED_3X, INVERSE, LEVERAGED_INVERSE}
)

# Short human-readable pill labels for the dashboard.
CATEGORY_LABELS: Dict[str, str] = {
    REGULAR: "",
    LEVERAGED_2X: "2x Leveraged",
    LEVERAGED_3X: "3x Leveraged",
    INVERSE: "Inverse",
    LEVERAGED_INVERSE: "Leveraged Inverse",
}

# Default (modifier, notional_cap_pct) per category.  The risk manager reads
# per-category Settings attributes and falls back to these, so the numbers live
# in one place and stay in sync with the spec:
#   regular          1.3x / 15%   (unchanged; handled by ETF_* settings)
#   leveraged 2x     0.5x / 5%
#   leveraged 3x     0.33x / 3%
#   inverse          0.7x / 5%
#   leveraged-inverse 0.25x / 2%
DEFAULT_RISK_PARAMS: Dict[str, Tuple[float, float]] = {
    LEVERAGED_2X: (0.5, 0.05),
    LEVERAGED_3X: (0.33, 0.03),
    INVERSE: (0.7, 0.05),
    LEVERAGED_INVERSE: (0.25, 0.02),
}


# ── Curated static map of known geared products ─────────────────────────────
# Not exhaustive, but covers the liquid names a retail bot is most likely to
# encounter.  Naming heuristics catch the long tail when a live ``info`` dict is
# available.
_KNOWN_LEVERAGED: Dict[str, str] = {
    # ── 3x long ──
    "TQQQ": LEVERAGED_3X,   # ProShares UltraPro QQQ
    "UPRO": LEVERAGED_3X,   # ProShares UltraPro S&P 500
    "SPXL": LEVERAGED_3X,   # Direxion Daily S&P 500 Bull 3X
    "TNA": LEVERAGED_3X,    # Direxion Small Cap Bull 3X
    "SOXL": LEVERAGED_3X,   # Direxion Semiconductor Bull 3X
    "TECL": LEVERAGED_3X,   # Direxion Technology Bull 3X
    "LABU": LEVERAGED_3X,   # Direxion Biotech Bull 3X
    "FAS": LEVERAGED_3X,    # Direxion Financial Bull 3X
    "UDOW": LEVERAGED_3X,   # ProShares UltraPro Dow30
    "TMF": LEVERAGED_3X,    # Direxion 20+Y Treasury Bull 3X
    "NUGT": LEVERAGED_3X,   # Direxion Gold Miners Bull 2X/3X
    "YINN": LEVERAGED_3X,   # Direxion China Bull 3X
    "FNGU": LEVERAGED_3X,   # MicroSectors FANG+ 3X
    "DPST": LEVERAGED_3X,   # Direxion Regional Banks Bull 3X
    "CURE": LEVERAGED_3X,   # Direxion Healthcare Bull 3X
    "NAIL": LEVERAGED_3X,   # Direxion Homebuilders Bull 3X
    # ── 3x inverse (leveraged-inverse) ──
    "SQQQ": LEVERAGED_INVERSE,  # ProShares UltraPro Short QQQ
    "SPXU": LEVERAGED_INVERSE,  # ProShares UltraPro Short S&P 500
    "SPXS": LEVERAGED_INVERSE,  # Direxion Daily S&P 500 Bear 3X
    "SDOW": LEVERAGED_INVERSE,  # ProShares UltraPro Short Dow30
    "TZA": LEVERAGED_INVERSE,   # Direxion Small Cap Bear 3X
    "SOXS": LEVERAGED_INVERSE,  # Direxion Semiconductor Bear 3X
    "TECS": LEVERAGED_INVERSE,  # Direxion Technology Bear 3X
    "LABD": LEVERAGED_INVERSE,  # Direxion Biotech Bear 3X
    "FAZ": LEVERAGED_INVERSE,   # Direxion Financial Bear 3X
    "TMV": LEVERAGED_INVERSE,   # Direxion 20+Y Treasury Bear 3X
    "DUST": LEVERAGED_INVERSE,  # Direxion Gold Miners Bear 2X/3X
    "YANG": LEVERAGED_INVERSE,  # Direxion China Bear 3X
    "FNGD": LEVERAGED_INVERSE,  # MicroSectors FANG+ 3X Inverse
    # ── 2x long ──
    "QLD": LEVERAGED_2X,    # ProShares Ultra QQQ
    "SSO": LEVERAGED_2X,    # ProShares Ultra S&P 500
    "DDM": LEVERAGED_2X,    # ProShares Ultra Dow30
    "UWM": LEVERAGED_2X,    # ProShares Ultra Russell2000
    "ROM": LEVERAGED_2X,    # ProShares Ultra Technology
    "UYG": LEVERAGED_2X,    # ProShares Ultra Financials
    "UVXY": LEVERAGED_2X,   # ProShares Ultra VIX Short-Term Futures (1.5x long vol)
    # ── 2x inverse (leveraged-inverse) ──
    "SDS": LEVERAGED_INVERSE,   # ProShares UltraShort S&P 500
    "QID": LEVERAGED_INVERSE,   # ProShares UltraShort QQQ
    "DXD": LEVERAGED_INVERSE,   # ProShares UltraShort Dow30
    "TWM": LEVERAGED_INVERSE,   # ProShares UltraShort Russell2000
    # ── 1x inverse ──
    "SH": INVERSE,     # ProShares Short S&P 500
    "PSQ": INVERSE,    # ProShares Short QQQ
    "DOG": INVERSE,    # ProShares Short Dow30
    "RWM": INVERSE,    # ProShares Short Russell2000
    "SVXY": INVERSE,   # ProShares Short VIX Short-Term Futures (-0.5x vol)
}

#: Public frozenset of every curated geared ticker.  A leveraged/inverse product
#: *is* an ETF, so :func:`config.etf_universe.is_etf` folds this into its static
#: recognition set — otherwise TQQQ/SQQQ/UVXY (absent from the plain-ETF list)
#: would be mis-sized as individual stocks.
KNOWN_GEARED_SYMBOLS: frozenset[str] = frozenset(_KNOWN_LEVERAGED)


def _has_inverse_token(text: str) -> bool:
    """Return whether *text* names an inverse/short product.

    ``"short-term"`` (a futures-maturity phrase, e.g. the VIX funds) must **not**
    trigger the inverse flag, so it is scrubbed before the token scan.
    """
    t = text.lower().replace("short-term", "").replace("short term", "")
    return any(
        tok in t
        for tok in ("inverse", "bear", "short", "-1x", "-2x", "-3x")
    )


def _classify_from_text(text: Optional[str]) -> Optional[str]:
    """Classify a leverage category from a fund *name* string, or ``None``.

    Recognises the standard sponsor naming conventions:
    ``Ultra`` (2×), ``UltraPro`` (3×), ``2X``/``3X``, ``Bull``/``Bear``,
    ``Inverse``/``Short``.  Returns ``None`` when the text shows no sign of
    gearing (i.e. a plain ETF).
    """
    if not text:
        return None
    t = text.lower()

    # Leverage factor.  Check the 3x markers before the 2x ones because
    # "ultrapro" contains "ultra".
    if "3x" in t or "3 x" in t or "ultrapro" in t:
        factor = 3
    elif "2x" in t or "2 x" in t or "ultra" in t:
        factor = 2
    else:
        factor = 1

    inverse = _has_inverse_token(t)

    if factor == 1 and not inverse:
        return None  # no gearing signal → treat as a regular ETF
    if inverse:
        return LEVERAGED_INVERSE if factor >= 2 else INVERSE
    return LEVERAGED_3X if factor >= 3 else LEVERAGED_2X


def classify_leverage(symbol: str, info: Optional[dict] = None) -> str:
    """Classify *symbol* into a leverage category.

    Resolution order (fail-open):

    1. The curated static :data:`_KNOWN_LEVERAGED` map (authoritative).
    2. Naming heuristics over the fund name in *info* (``longName`` /
       ``shortName``) when a live ``yfinance`` ``info`` dict is supplied.

    Args:
        symbol: Ticker string (case-insensitive).
        info: Optional ``yfinance`` ``Ticker.info``-shaped dict.  When present,
            its fund name (and any explicit ``leverageFactor``) is inspected.

    Returns:
        One of :data:`ALL_CATEGORIES`; :data:`REGULAR` for plain / unknown ETFs.
    """
    sym = str(symbol or "").upper().strip()
    if not sym:
        return REGULAR

    known = _KNOWN_LEVERAGED.get(sym)
    if known is not None:
        return known

    if info:
        # Some providers expose an explicit numeric leverage factor.
        cat = _classify_from_info_factor(info)
        if cat is not None:
            return cat
        name = str(info.get("longName") or info.get("shortName") or "")
        cat = _classify_from_text(name)
        if cat is not None:
            return cat

    return REGULAR


def _classify_from_info_factor(info: dict) -> Optional[str]:
    """Classify from an explicit numeric leverage factor if the dict exposes one.

    Looks for a ``leverageFactor`` / ``leverage`` field (signed: negative means
    inverse).  Returns ``None`` when no usable numeric factor is present, so the
    caller falls through to name-based heuristics.
    """
    raw = info.get("leverageFactor", info.get("leverage"))
    try:
        factor = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if factor == 0:
        return None
    inverse = factor < 0
    mag = abs(factor)
    if mag <= 1.0 + 1e-9:
        return INVERSE if inverse else REGULAR
    if inverse:
        return LEVERAGED_INVERSE
    return LEVERAGED_3X if mag >= 2.5 else LEVERAGED_2X


def is_geared(symbol: str, info: Optional[dict] = None) -> bool:
    """Return ``True`` when *symbol* is a leveraged or inverse product."""
    return classify_leverage(symbol, info) in GEARED_CATEGORIES


def leverage_label(category: str) -> str:
    """Return a short UI label for *category* (empty string for regular)."""
    return CATEGORY_LABELS.get(category, "")


def default_risk_params(category: str) -> Optional[Tuple[float, float]]:
    """Return the default ``(modifier, notional_cap_pct)`` for a geared category.

    Returns ``None`` for :data:`REGULAR` (the caller keeps the standard ETF
    parameters in that case).
    """
    return DEFAULT_RISK_PARAMS.get(category)
