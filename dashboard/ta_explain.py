"""
Deterministic plain-English explanations for the TA chart panel (TA1).

Every sentence is a template filled from persisted or freshly computed
values — **no LLM is involved anywhere in this module** — so the text is
exact, free, and can never hallucinate a number.  The chart and the prose
always agree because both render the same payload fields.

Pure functions, no I/O: :func:`build_explanation` takes the trade dict,
the snapshot ``state`` flags, ATR, S/R levels, and (for open positions)
live readings, and returns the ``explanation`` block for the API payload.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from journal.rationale import _STRATEGY_PATTERN

#: What each grade means for sizing (SYSTEM_DESIGN.md section 5.4).
_GRADE_MEANING = {
    "A": "traded at full position size",
    "B": "traded at 75% position size",
    "C": "normally skipped (insufficient quality)",
    "F": "normally blocked (hard veto or very weak)",
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def entry_explanation(trade: Dict[str, Any], state: Dict[str, Any]) -> str:
    """Which indicators triggered the entry, as one readable sentence."""
    strength = _f(trade.get("signal_strength") or trade.get("overall_score"))
    if trade.get("overall_score") is not None and not trade.get("signal_strength"):
        strength = strength / 10.0  # rationale stores 0-10, signal 0-1
    grade = str(trade.get("grade") or "?")
    lead = f"Entry signal: combined score {strength:.2f} (grade {grade})."

    clauses: List[str] = []

    rsi = state.get("rsi_value", trade.get("rsi_value"))
    if rsi is not None:
        if state.get("rsi_momentum_zone"):
            clauses.append(
                f"RSI {_f(rsi):.0f} in the momentum zone (55–70)"
            )
        elif state.get("rsi_rising"):
            clauses.append(f"RSI {_f(rsi):.0f} and rising")
        else:
            clauses.append(f"RSI {_f(rsi):.0f}")

    if state.get("macd_confirmed_bullish"):
        clauses.append("MACD bullish crossover confirmed above zero")
    elif state.get("macd_bullish_crossover"):
        clauses.append("MACD crossed above its signal line")
    elif state.get("macd_momentum_intact"):
        clauses.append("MACD momentum intact (positive, expanding histogram)")
    else:
        hist = state.get("macd_histogram", trade.get("macd_histogram"))
        if hist is not None:
            clauses.append(
                f"MACD histogram {_f(hist):+.3f} "
                f"({'positive' if _f(hist) > 0 else 'negative'})"
            )

    vol_ratio = state.get("volume_ratio", trade.get("volume_ratio"))
    if vol_ratio is not None and _f(vol_ratio) > 0:
        surge = " (bullish surge)" if state.get("volume_surge") else ""
        clauses.append(
            f"volume {_f(vol_ratio):.1f}× its 20-day average{surge}"
        )

    if state.get("bullish_stack"):
        clauses.append("price above all four EMAs (9/20/50/200)")
    elif state.get("partial_stack"):
        clauses.append("price above the 20 and 50 EMAs")
    elif state.get("above_ema200"):
        clauses.append("price above the 200 EMA (bull regime)")

    if state.get("obv_confirming"):
        clauses.append("OBV confirming the trend")

    if not clauses:
        return lead
    joined = "; ".join(clauses)
    return lead + " " + joined[0].upper() + joined[1:] + "."


def stop_explanation(
    entry: float,
    stop: float,
    atr: Optional[float],
    atr_multiplier: float,
) -> str:
    """The stop-loss formula with the actual ATR value from the snapshot."""
    if not stop:
        return "No stop price recorded for this trade."
    if atr and atr > 0 and entry > 0:
        derived = entry - atr_multiplier * atr
        # Only present the formula when it actually reproduces the stop
        # (manual trades and laddered exits set their own levels).
        if abs(derived - stop) / entry < 0.005:
            return (
                f"ATR-based stop: entry ${entry:,.2f} − "
                f"{atr_multiplier:g} × ATR(14) ${atr:,.2f} = "
                f"${stop:,.2f} (settings ATR_STOP_MULTIPLIER)."
            )
    pct = abs(entry - stop) / entry * 100.0 if entry > 0 else 0.0
    note = f" Current ATR(14) is ${atr:,.2f}." if atr and atr > 0 else ""
    return (
        f"Stop ${stop:,.2f} — {pct:.1f}% below the "
        f"${entry:,.2f} entry (custom level, not the standard "
        f"ATR formula).{note}"
    )


def target_explanation(
    entry: float,
    stop: float,
    target: float,
    rr_min: float,
    resistance: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """The target formula with the realised risk/reward ratio."""
    if not target:
        return "No target price recorded for this trade."
    risk = entry - stop if stop else 0.0
    if risk > 0:
        rr = (target - entry) / risk
        text = (
            f"Target ${target:,.2f} = entry ${entry:,.2f} + "
            f"(entry − stop) × {rr:.1f} — "
            f"risk/reward {rr:.1f}:1 (minimum {rr_min:g}:1)."
        )
    else:
        text = f"Target ${target:,.2f}."
    for level in resistance or []:
        price = _f(level.get("price"))
        if entry < price < target:
            touches = int(level.get("touches", 1) or 1)
            plural = "touch" if touches == 1 else "touches"
            text += (
                f" Note: nearest resistance ${price:,.2f} "
                f"({touches} {plural}) sits before the target."
            )
            break
    return text


def setup_explanation(grade: str, strategy: str) -> str:
    """Grade + named pattern, with what the grade means for sizing."""
    strategy_key = str(strategy or "").lower()
    pattern = _STRATEGY_PATTERN.get(
        strategy_key, f"{strategy_key or 'unknown'} setup"
    )
    grade = str(grade or "?").upper()
    meaning = _GRADE_MEANING.get(grade)
    text = f"Grade {grade} {pattern}."
    if meaning:
        text += f" Grade {grade} setups are {meaning}."
    return text


def current_explanation(live: Dict[str, Any]) -> str:
    """Current readings for an open position (from live indicators + P&L)."""
    parts: List[str] = []
    price = live.get("price")
    if price is not None:
        chg = live.get("change_pct")
        chg_txt = f" ({_f(chg):+.1f}%)" if chg is not None else ""
        parts.append(f"price ${_f(price):,.2f}{chg_txt}")
    if live.get("rsi") is not None:
        parts.append(f"RSI {_f(live['rsi']):.0f}")
    hist = live.get("macd_hist")
    if hist is not None:
        parts.append(
            f"MACD histogram {'positive' if _f(hist) > 0 else 'negative'} "
            f"({_f(hist):+.3f})"
        )
    pct20 = live.get("pct_above_ema20")
    if pct20 is not None:
        rel = "above" if _f(pct20) >= 0 else "below"
        parts.append(f"{abs(_f(pct20)):.1f}% {rel} the 20 EMA")
    r = live.get("r_progress")
    if r is not None:
        parts.append(f"{_f(r):+.2f}R so far")
    ds, dt = live.get("distance_to_stop_pct"), live.get("distance_to_target_pct")
    if ds is not None and dt is not None:
        parts.append(f"{_f(ds):.1f}% from stop, {_f(dt):.1f}% from target")
    if not parts:
        return ""
    return "Now: " + "; ".join(parts) + "."


def ai_note_from_criteria(
    criteria: Optional[List[Dict[str, Any]]],
) -> Optional[str]:
    """The persisted AI-veto verdict line from the F9 criteria, if any."""
    for c in criteria or []:
        if c.get("key") == "ai_veto":
            explanation = str(c.get("explanation") or "").strip()
            return f"AI veto: {explanation}" if explanation else None
    return None


def build_explanation(
    trade: Dict[str, Any],
    state: Dict[str, Any],
    atr: Optional[float],
    levels: Dict[str, Any],
    *,
    atr_multiplier: float,
    rr_min: float,
    live: Optional[Dict[str, Any]] = None,
    criteria: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Optional[str]]:
    """Assemble the full explanation block for the TA chart payload."""
    entry = _f(trade.get("entry_price"))
    stop = _f(trade.get("stop_price"))
    target = _f(trade.get("target_price"))
    return {
        "entry": entry_explanation(trade, state or {}),
        "stop": stop_explanation(entry, stop, atr, atr_multiplier),
        "target": target_explanation(
            entry, stop, target, rr_min, (levels or {}).get("resistance")
        ),
        "setup": setup_explanation(
            str(trade.get("grade") or ""), str(trade.get("strategy") or "")
        ),
        "current": current_explanation(live) if live else None,
        "ai_note": ai_note_from_criteria(criteria),
    }
