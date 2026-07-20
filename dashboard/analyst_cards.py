"""
Analyst card builder — the self-explanatory card payload (Analyst UX spec).

Turns the computed per-symbol facts (indicators, key levels, position risk
math) into the fixed card structure every Analyst card renders, per the UX
spec's component contract (section 8.2):

    identity -> summary -> conditions met -> reasoning walkthrough ->
    what would change this -> stress test -> collapsed detail

Design rules implemented here (spec sections 2, 5, 6, 7):

* Plain language leads; the technical stat trails as supporting evidence.
* No verdict badges and no bare numbers — every figure is paired with what
  it means ("3 of 5 conditions met", never "60%").
* Every stress-test number is traceable: each scenario carries the worked
  calculation ("working") a user can check by hand.
* Degraded/unavailable states are per-card and explain themselves.

All strings live in :data:`PLAIN_LANGUAGE` (spec 8.1's shared content
source) so wording cannot drift between cards or screens.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Shared plain-language content source (spec 8.1).
#
# One entry per indicator / term: a short label, plain-language templates,
# and a one-sentence definition used by the inline info icon.  Referenced
# everywhere a term appears so the same word always means the same thing.
# ---------------------------------------------------------------------------

PLAIN_LANGUAGE: Dict[str, Dict[str, Any]] = {
    "trend": {
        "label": "Trend",
        "define": "Where price sits versus its recent averages — analysts "
                  "use moving averages to judge direction.",
    },
    "momentum": {
        "label": "Momentum",
        "define": "How much force is behind the current move. RSI "
                  "(relative strength index) reads 0–100; above 70 usually "
                  "counts as stretched, below 30 as washed out.",
    },
    "volume": {
        "label": "Volume",
        "define": "How many shares are trading versus normal. Moves on "
                  "heavy volume carry more conviction than quiet ones.",
    },
    "structure": {
        "label": "Structure",
        "define": "Price levels where buyers (support) or sellers "
                  "(resistance) have repeatedly stepped in before.",
    },
    "moving_average": {
        "label": "20-day average",
        "define": "The average closing price of the last 20 trading days — "
                  "a level analysts use to judge short-term direction.",
    },
    "rsi": {
        "label": "RSI",
        "define": "Relative strength index: a 0–100 momentum gauge. Above "
                  "70 usually reads as stretched, below 30 as washed out.",
    },
    "macd": {
        "label": "MACD",
        "define": "A momentum gauge comparing a fast and a slow average of "
                  "price; a growing positive reading means the move is "
                  "gaining force.",
    },
    "support": {
        "label": "Support",
        "define": "A price where buyers have repeatedly stepped in; a "
                  "close below it means that floor has given way.",
    },
    "resistance": {
        "label": "Resistance",
        "define": "A price where sellers have repeatedly stepped in; a "
                  "close above it opens room higher.",
    },
    "r_multiple": {
        "label": "R-multiple",
        "define": "Profit or loss measured in units of the risk taken at "
                  "entry: +2R means the trade made twice what it risked.",
    },
    "stop": {
        "label": "Stop",
        "define": "The exit price that caps the loss — the bot sells "
                  "(or covers) automatically if price reaches it.",
    },
    "target": {
        "label": "Target",
        "define": "The price where the bot plans to take profit.",
    },
    "hypothetical": {
        "label": "Hypothetical — no position open",
        "define": "No money is in this trade. The numbers show what WOULD "
                  "happen if the suggested trade were taken at the "
                  "suggested size.",
    },
}


def term_help(key: str) -> str:
    """One-sentence definition for the inline info icon."""
    return str(PLAIN_LANGUAGE.get(key, {}).get("define", ""))


# Strategy id -> plain-language card tag (identity strip; spec section 4.1).
STRATEGY_TAGS: Dict[str, str] = {
    "momentum": "Momentum position",
    "swing": "Swing position",
    "vcp_breakout": "Breakout position",
    "pead": "Earnings-drift position",
    "mean_reversion": "Rebound position",
    "sector_rotation": "Sector-rotation position",
    "short_gap_fail": "Short position — failed gap",
    "short_earnings_pop_fade": "Short position — earnings fade",
    "short_support_breakdown": "Short position — broken support",
    "short_bear_flag": "Short position — bear flag",
    "short_buying_climax": "Short position — buying climax",
    "short_overbought_fade": "Short position — overbought fade",
    "short_vwap_rejection": "Short position — VWAP rejection",
    "short_ma_crossunder": "Short position — average crossed down",
    "short_relative_weakness": "Short position — weakest in group",
    "short_laggard_fade": "Short position — sector laggard",
    # Highly selective strategies
    "hs_rsi2_reversal": "Selective — RSI-2 reversal at structure",
    "hs_triple_timeframe": "Selective — triple-timeframe breakout",
    "hs_bb_climax": "Selective — Bollinger climax reversal",
    "hs_pead_drift": "Selective — post-earnings drift",
    "hs_gap_fill": "Selective — statistical gap fade",
    "hs_turnaround_tuesday": "Selective — Turnaround Tuesday",
}


def build_ratings(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Render a card's third-party ratings block (Feature 5), or ``None``.

    Reads a pre-fetched ratings snapshot from ``row["ratings"]`` (a dict shaped
    like :meth:`data.ratings.RatingSnapshot.to_dict`) so card assembly stays
    free of network I/O — the router attaches the (TTL-cached) snapshot when the
    ratings feature is enabled.  Fail-open: returns ``None`` when absent so the
    UI shows "—".
    """
    snap = row.get("ratings")
    if not isinstance(snap, dict) or not snap.get("quant_rating"):
        return None
    price = row.get("price") or row.get("current_price")
    target = snap.get("price_target")
    upside = None
    try:
        if target and price and float(price) > 0:
            upside = round((float(target) / float(price) - 1.0) * 100.0, 2)
    except (TypeError, ValueError):
        upside = None
    return {
        "quant_rating": snap.get("quant_rating"),
        "quant_rating_rank": snap.get("quant_rating_rank"),
        "consensus": snap.get("consensus"),
        "factor_grades": snap.get("factor_grades") or {},
        "price_target": target,
        "upside_pct": upside,
        "recent_change": snap.get("recent_change"),
        "as_of": snap.get("as_of"),
    }


def _asset_type(symbol: Optional[str]) -> str:
    """Return ``"etf"`` or ``"stock"`` for a card's identity (fail-safe)."""
    try:
        from config.etf_universe import asset_type

        return asset_type(str(symbol or ""))
    except Exception:  # noqa: BLE001
        return "stock"


def _leverage_meta(symbol: Optional[str]) -> Dict[str, str]:
    """Return ``{"leverage", "leverage_label"}`` for a card identity (fail-safe).

    Pure/static classification (no network), so it is cheap enough to run for
    every card.  ``leverage_label`` is empty for plain / unknown ETFs, which the
    UI reads as "no warning pill".
    """
    try:
        from config.etf_classification import classify_leverage, leverage_label

        category = classify_leverage(str(symbol or ""))
        return {"leverage": category, "leverage_label": leverage_label(category)}
    except Exception:  # noqa: BLE001
        return {"leverage": "regular", "leverage_label": ""}


def build_etf_info(symbol: Optional[str], asset_type: str) -> Optional[Dict[str, Any]]:
    """Return the *ETF Info* card section for an ETF, or ``None``.

    Only populated when *asset_type* is ``"etf"``; fetches TTL-cached fund
    fundamentals (expense ratio, NAV, category, fund family, top-10 holdings,
    leverage) via :func:`data.etf_metadata.get_etf_info`.  Fail-open — any error
    or missing data returns ``None`` so the section is simply hidden.
    """
    if asset_type != "etf" or not symbol:
        return None
    try:
        from data.etf_metadata import get_etf_info

        info = get_etf_info(str(symbol))
        if info is None:
            return None
        return info.to_dict()
    except Exception:  # noqa: BLE001
        return None


def strategy_tag(strategy: str, is_position: bool) -> str:
    tag = STRATEGY_TAGS.get(str(strategy or "").lower())
    if tag:
        return tag if is_position else tag.replace(" position", " setup")
    return "Open position" if is_position else "Watchlist"


def _fmt(value: Optional[float], decimals: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{decimals}f}"


def _money(value: Optional[float]) -> str:
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


# ---------------------------------------------------------------------------
# Conditions met (spec section 4.3) — the strategy's own screening
# conditions, shown as a segmented "N of M" bar instead of a confidence %.
# ---------------------------------------------------------------------------


def build_conditions(ind: Optional[Dict[str, Any]],
                     is_short: bool) -> Optional[Dict[str, Any]]:
    """The five screening conditions the scanner scores, as met/not-met.

    Mirrors the scoring components (RSI, MACD, price-vs-averages, volume,
    trend clouds) so the bar reflects exactly what the strategy checks —
    identical to the screen, not a curated subset (spec open question 2).
    """
    if not ind:
        return None

    rsi = ind.get("rsi")
    if is_short:
        items = [
            {
                "key": "trend",
                "label": "Trend pointing down",
                "met": not ind.get("above_ema20", True),
                "plain": "Price is below its 20-day average"
                         if not ind.get("above_ema20", True)
                         else "Price is still above its 20-day average",
            },
            {
                "key": "momentum",
                "label": "Momentum fading",
                "met": bool(rsi is not None and (rsi < 50 or ind.get("rsi_overbought"))),
                "plain": "Buying momentum is fading or stretched",
            },
            {
                "key": "macd",
                "label": "Downward pressure",
                "met": not ind.get("macd_bullish", True),
                "plain": "The MACD momentum gauge points down"
                         if not ind.get("macd_bullish", True)
                         else "The MACD momentum gauge still points up",
            },
            {
                "key": "volume",
                "label": "Volume behind the move",
                "met": bool((ind.get("volume_ratio") or 0) >= 1.0),
                "plain": "Selling is happening on above-average volume"
                         if (ind.get("volume_ratio") or 0) >= 1.0
                         else "Volume is below its recent average",
            },
            {
                "key": "clouds",
                "label": "Trend clouds red",
                "met": not ind.get("fast_cloud_bullish", True)
                       and not ind.get("slow_cloud_bullish", True),
                "plain": "Both short- and medium-term trend gauges are "
                         "negative",
            },
        ]
    else:
        items = [
            {
                "key": "trend",
                "label": "Trend pointing up",
                "met": bool(ind.get("above_ema20")),
                "plain": "Price is above its 20-day average"
                         if ind.get("above_ema20")
                         else "Price has slipped below its 20-day average",
            },
            {
                "key": "momentum",
                "label": "Momentum healthy",
                "met": bool(ind.get("rsi_momentum_zone")
                            and not ind.get("rsi_overbought")),
                "plain": "Momentum is building without looking stretched",
            },
            {
                "key": "macd",
                "label": "Upward pressure",
                "met": bool(ind.get("macd_bullish")),
                "plain": "The MACD momentum gauge points up"
                         if ind.get("macd_bullish")
                         else "The MACD momentum gauge has rolled over",
            },
            {
                "key": "volume",
                "label": "Volume confirming",
                "met": bool((ind.get("volume_ratio") or 0) >= 1.0
                            or ind.get("obv_confirming")),
                "plain": "Buying is backed by healthy volume"
                         if ((ind.get("volume_ratio") or 0) >= 1.0
                             or ind.get("obv_confirming"))
                         else "Volume is quiet, not confirming strongly",
            },
            {
                "key": "clouds",
                "label": "Trend clouds green",
                "met": bool(ind.get("fast_cloud_bullish")
                            and ind.get("slow_cloud_bullish")),
                "plain": "Both short- and medium-term trend gauges are "
                         "positive",
            },
        ]
    met = sum(1 for i in items if i["met"])
    return {"met": met, "total": len(items), "items": items}


# ---------------------------------------------------------------------------
# Reasoning walkthrough (spec section 4.4) — trend, momentum, volume,
# structure; plain language first, the supporting stat underneath.
# ---------------------------------------------------------------------------


def build_reasoning(ind: Optional[Dict[str, Any]],
                    levels: Optional[Dict[str, Any]],
                    is_short: bool) -> List[Dict[str, Any]]:
    if not ind:
        return []
    out: List[Dict[str, Any]] = []

    # Trend.
    above20 = bool(ind.get("above_ema20"))
    above200 = bool(ind.get("above_ema200"))
    if above20 and above200:
        trend_plain = "Trend: still pointing up."
    elif not above20 and not above200:
        trend_plain = "Trend: pointing down."
    elif above200:
        trend_plain = "Trend: up overall, but easing short-term."
    else:
        trend_plain = "Trend: bouncing short-term inside a longer downtrend."
    out.append({
        "category": "trend",
        "plain_statement": trend_plain,
        "supporting_stat": (
            f"Price is {'above' if above20 else 'below'} its 20-day average "
            f"(${_fmt(ind.get('ema20'))}) and "
            f"{'above' if above200 else 'below'} its 200-day average "
            f"(${_fmt(ind.get('ema200'))})."
        ),
        "term_help": term_help("moving_average"),
    })

    # Momentum.
    rsi = ind.get("rsi")
    rising = bool(ind.get("rsi_rising"))
    overbought = bool(ind.get("rsi_overbought"))
    if rsi is None:
        mom_plain = "Momentum: not enough data."
        mom_stat = ""
    elif overbought:
        mom_plain = "Momentum: strong but stretched."
        mom_stat = (f"RSI at {rsi:.0f}; above 70 usually reads as "
                    "stretched — moves from here often pause or pull back.")
    elif is_short:
        mom_plain = ("Momentum: fading." if rsi < 50
                     else "Momentum: still positive — works against this "
                          "short.")
        mom_stat = (f"RSI at {rsi:.0f} and "
                    f"{'rising' if rising else 'easing'}; below 50 means "
                    "sellers have the upper hand.")
    else:
        mom_plain = ("Momentum: building, not stretched." if rsi >= 50
                     else "Momentum: soft — buyers not in control yet.")
        mom_stat = (f"RSI at {rsi:.0f} and "
                    f"{'rising' if rising else 'easing'}; 70+ is usually "
                    "\"stretched\".")
    out.append({
        "category": "momentum",
        "plain_statement": mom_plain,
        "supporting_stat": mom_stat,
        "term_help": term_help("rsi"),
    })

    # Volume.
    vr = ind.get("volume_ratio")
    if vr is None:
        vol_plain, vol_stat = "Volume: not enough data.", ""
    elif vr >= 1.5:
        vol_plain = "Volume: heavy — the move has conviction."
        vol_stat = f"{vr:.1f}x its 20-day average volume."
    elif vr >= 1.0:
        vol_plain = "Volume: about normal."
        vol_stat = f"{vr:.1f}x its 20-day average volume."
    else:
        vol_plain = "Volume: quiet, not confirming strongly."
        vol_stat = f"{vr:.1f}x its 20-day average volume."
    out.append({
        "category": "volume",
        "plain_statement": vol_plain,
        "supporting_stat": vol_stat,
        "term_help": term_help("volume"),
    })

    # Structure.
    sup = ((levels or {}).get("support") or [None])[0]
    res = ((levels or {}).get("resistance") or [None])[0]
    if sup or res:
        pieces = []
        if sup:
            pieces.append(f"above support at ${_fmt(sup['price'])}")
        if res:
            pieces.append(f"below resistance at ${_fmt(res['price'])}")
        struct_plain = f"Structure: {' and '.join(pieces)}."
        stat_bits = []
        if sup:
            stat_bits.append(
                f"Support ${_fmt(sup['price'])} has held "
                f"{sup.get('touches', 1)} time(s) — a level buyers have "
                "repeatedly stepped in at."
            )
        if res:
            stat_bits.append(
                f"Resistance ${_fmt(res['price'])} "
                f"({res.get('touches', 1)} touch(es))."
            )
        struct_stat = " ".join(stat_bits)
    else:
        struct_plain = "Structure: no well-tested nearby levels."
        struct_stat = ("No price level in the last few months has been "
                       "tested often enough to lean on.")
    out.append({
        "category": "structure",
        "plain_statement": struct_plain,
        "supporting_stat": struct_stat,
        "term_help": term_help("support"),
    })
    return out


# ---------------------------------------------------------------------------
# "What would change this" (spec section 4.5).
# ---------------------------------------------------------------------------


def build_invalidation(ind: Optional[Dict[str, Any]],
                       levels: Optional[Dict[str, Any]],
                       stop: Optional[float],
                       is_short: bool) -> List[str]:
    out: List[str] = []
    sup = ((levels or {}).get("support") or [None])[0]
    res = ((levels or {}).get("resistance") or [None])[0]
    if is_short:
        if res:
            out.append(
                f"A close above ${_fmt(res['price'])} breaks the resistance "
                f"that has capped price {res.get('touches', 1)} time(s) — "
                "the case for this short weakens."
            )
        if stop:
            out.append(
                f"Price reaching the stop at ${_fmt(stop)} ends the trade — "
                "the bot buys back the shares automatically."
            )
        if ind and ind.get("rsi") is not None:
            out.append(
                "A momentum turn (RSI pushing back above 50 on rising "
                "volume) would say buyers are back in control."
            )
    else:
        if sup:
            out.append(
                f"A close below ${_fmt(sup['price'])} breaks the support "
                f"that has held {sup.get('touches', 1)} time(s)."
            )
        if stop:
            out.append(
                f"Price reaching the stop at ${_fmt(stop)} ends the trade — "
                "the bot sells automatically."
            )
        if ind and ind.get("rsi") is not None:
            out.append(
                "RSI above 80 while volume fades is an exhaustion signal — "
                "strong moves often stall there."
            )
    return out


# ---------------------------------------------------------------------------
# Stress test (spec section 5) — what-if P&L at each key level, with the
# arithmetic shown.  A what-if calculator, not a prediction.
# ---------------------------------------------------------------------------


def _scenario(label: str, price: float, entry: float, shares: int,
              risk_dollars: float, is_short: bool,
              note: str = "") -> Dict[str, Any]:
    sign = -1.0 if is_short else 1.0
    move = (price - entry) * sign
    pnl = move * shares
    notional = entry * shares
    pnl_pct = (pnl / notional * 100.0) if notional else 0.0
    r_multiple = (pnl / risk_dollars) if risk_dollars > 0 else 0.0

    direction_word = "short sale" if is_short else "purchase"
    working = [
        f"Shares {'sold short' if is_short else 'held'}: {shares}",
        f"Entry price ({direction_word}): ${entry:,.2f}",
        f"Scenario price: ${price:,.2f}",
        "----------------------------------------",
    ]
    if is_short:
        working.append(
            f"Price change (gain when price falls): "
            f"${entry:,.2f} - ${price:,.2f} = ${entry - price:,.2f}"
        )
        working.append(
            f"P&L ($): ${entry - price:,.2f} x {shares} = {_money(pnl)}"
        )
    else:
        working.append(
            f"Price change: ${price:,.2f} - ${entry:,.2f} = "
            f"${price - entry:,.2f}"
        )
        working.append(
            f"P&L ($): ${price - entry:,.2f} x {shares} = {_money(pnl)}"
        )
    working.append(
        f"P&L (%): {_money(pnl)} / ${notional:,.2f} = {pnl_pct:+.1f}%"
    )
    if risk_dollars > 0:
        working.append(
            f"Risk taken at entry (entry to stop): {_money(risk_dollars)}"
        )
        working.append(
            f"R-multiple: {_money(pnl)} / {_money(risk_dollars)} = "
            f"{r_multiple:+.1f}R"
        )
    if note:
        working.append(note)
    return {
        "label": label,
        "trigger_price": round(price, 4),
        "pnl_dollars": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "r_multiple": round(r_multiple, 2),
        "working": working,
    }


def build_stress_test(
    entry: Optional[float],
    shares: Optional[int],
    stop: Optional[float],
    target: Optional[float],
    current: Optional[float],
    levels: Optional[Dict[str, Any]],
    is_short: bool,
    is_hypothetical: bool,
    sizing_note: str = "",
) -> Optional[Dict[str, Any]]:
    """The five default scenarios (spec 5.1): stop, support, no change,
    resistance, target — levels already surfaced elsewhere on the card.

    Returns ``None`` when there is no entry/stop to anchor the math (the
    card explains why instead of showing a blank section).
    """
    if not entry or entry <= 0 or not stop or stop <= 0:
        return None
    shares = int(shares or 0)
    zero_shares = shares <= 0
    if zero_shares:
        # Spec 5.5: still show the per-share math, with a note explaining
        # that the share count is what makes the trade too small to place.
        shares = 1
    # Initial risk defined at entry: |entry - stop| x shares.
    risk_dollars = abs((stop - entry) * shares)

    sup = ((levels or {}).get("support") or [None])[0]
    res = ((levels or {}).get("resistance") or [None])[0]

    scenarios: List[Dict[str, Any]] = []
    scenarios.append(_scenario(
        "Hits stop-loss", float(stop), entry, shares, risk_dollars, is_short,
        note="This is the most the trade is designed to lose.",
    ))
    if sup:
        scenarios.append(_scenario(
            "Drops to support", float(sup["price"]), entry, shares,
            risk_dollars, is_short,
        ))
    if current and current > 0:
        flat = _scenario("No change", float(current), entry, shares,
                         risk_dollars, is_short)
        flat["label"] = "No change"
        if is_hypothetical:
            flat["working"].append(
                "Baseline: the hypothetical trade starts here, so no "
                "gain or loss yet."
            )
        else:
            flat["working"].append(
                "Baseline: this is the trade's current unrealized result."
            )
        scenarios.append(flat)
    if res:
        scenarios.append(_scenario(
            "Rallies to resistance", float(res["price"]), entry, shares,
            risk_dollars, is_short,
        ))
    if target and target > 0:
        scenarios.append(_scenario(
            "Hits target", float(target), entry, shares, risk_dollars,
            is_short,
            note="This is where the bot plans to take profit.",
        ))

    # Ordered along the price line, lowest trigger first, so the UI can
    # draw them left-to-right without re-sorting.
    scenarios.sort(key=lambda s: s["trigger_price"])

    notes: List[str] = []
    if is_short:
        notes.append(
            "This is a short position: a FALL in price is a gain and a "
            "RISE is a loss — the opposite of a normal (long) position."
        )
    if zero_shares and sizing_note:
        notes.append(sizing_note)
    elif zero_shares:
        notes.append(
            "Shown per single share: at the strategy's risk settings the "
            "position size rounds down to zero shares, which is why the "
            "bot did not place this trade automatically."
        )
    notes.append(
        "A what-if calculator, not a prediction: it shows outcomes, never "
        "how likely they are."
    )

    return {
        "is_hypothetical": bool(is_hypothetical),
        "entry_price": round(float(entry), 4),
        "shares": shares,
        "shares_are_per_share_illustration": zero_shares,
        "stop_price": round(float(stop), 4),
        "target_price": round(float(target), 4) if target else None,
        "current_price": round(float(current), 4) if current else None,
        "direction": "short" if is_short else "long",
        "scenarios": scenarios,
        "custom_input_enabled": True,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Summary lines (spec section 4.2) — one plain-English sentence that stands
# alone; a reader who reads only this still gets the gist.
# ---------------------------------------------------------------------------


def _position_summary(row: Dict[str, Any], is_short: bool) -> str:
    dist_stop = row.get("distance_to_stop_pct")
    dist_target = row.get("distance_to_target_pct")
    pnl = row.get("unrealized_pnl")
    if dist_stop is not None and dist_stop <= 1.5:
        return ("Price is within a whisker of the exit level that caps "
                "this trade's loss — the next move decides it.")
    if dist_target is not None and dist_target <= 1.5:
        return ("Price is closing in on the level where the bot plans to "
                "take profit.")
    if is_short:
        if pnl is not None and pnl >= 0:
            return ("Price has fallen since this short was opened, and "
                    "nothing yet suggests buyers are taking back control.")
        return ("Price has moved against this short so far, but it hasn't "
                "reached the level that would end the trade.")
    if pnl is not None and pnl >= 0:
        return ("Price is still above the level that triggered this trade, "
                "and the trend hasn't broken down.")
    return ("Price has slipped since entry, but it hasn't broken the "
            "levels that would end the trade.")


def _watchlist_summary(row: Dict[str, Any]) -> str:
    status = str(row.get("status", "idle"))
    sig = row.get("signal") or {}
    if status == "signal":
        return ("The bot found a setup here on its last scan and has a "
                "suggested entry, exit, and profit level ready.")
    if status == "near_entry":
        return ("A setup exists, but price drifted from where the bot "
                "wanted to enter — it re-checks on the next scan.")
    if status == "rejected":
        return ("The bot looked at this symbol recently and decided not "
                "to trade it — the reason is spelled out below.")
    if status == "excluded":
        return ("This symbol is outside your current trade selection, so "
                "the bot is not scanning it for entries.")
    if sig:
        return "The bot is watching this symbol for a tradeable setup."
    return ("Nothing actionable here yet — the bot keeps watching for a "
            "setup on every scan.")


# ---------------------------------------------------------------------------
# Rejection reasons in plain language (spec 6/5.4: states explain
# themselves; never a raw system message).
# ---------------------------------------------------------------------------

_GATE_EXPLANATIONS: Dict[str, str] = {
    "pre_check": "it failed the basic safety checks (position limits, "
                 "recent loss limits, or the trade's risk/reward was too "
                 "thin)",
    "strategy_cap": "the bot already holds as many trades of this type as "
                    "its rules allow",
    "pending_order_guard": "the bot already holds or is entering this "
                           "symbol",
    "earnings_filter": "an earnings report is due within the blackout "
                       "window, so the bot is holding off to avoid the "
                       "binary gap risk",
    "ai_veto": "the AI review flagged a risk (often nearby earnings) and "
               "vetoed the entry",
    "ratings_filter": "a third-party quant rating for this stock is below "
                      "the minimum you set",
    "gap_filter": "the price gapped sharply overnight, so the bot skipped "
                  "or resized the entry",
    "news_sentiment": "recent news about this company was strongly "
                      "negative",
    "regime_autotune": "market conditions currently disfavour this type "
                       "of trade, so only the strongest setups qualify",
    "trade_selection": "your trade selection settings exclude it",
    "freshness_check": "price moved away from the setup before the order "
                       "could be placed",
    "order_build": "the position size worked out to zero shares at the "
                   "configured risk settings — the trade is too small to "
                   "place, not an error",
    "cash_check": "there wasn't enough uncommitted cash to fund it",
}


def explain_rejection(rej: Optional[Dict[str, Any]]) -> Optional[str]:
    if not rej:
        return None
    gate = str(rej.get("gate", "") or "")
    plain = _GATE_EXPLANATIONS.get(gate)
    if plain:
        return f"Skipped on the last scan because {plain}."
    detail = str(rej.get("detail", "") or "").strip()
    if "zero shares" in detail:
        return (f"Skipped on the last scan because "
                f"{_GATE_EXPLANATIONS['order_build']}.")
    if detail:
        return f"Skipped on the last scan: {detail}."
    return "Skipped on the last scan."


# ---------------------------------------------------------------------------
# Hypothetical sizing for watchlist stress tests (spec 5.5): the same
# sizing rule the bot would apply, not an arbitrary round number.
# ---------------------------------------------------------------------------


def hypothetical_shares(entry: float, stop: float, grade: Optional[str],
                        is_short: bool, settings) -> int:
    """Mirror RiskManager/backtest sizing for a would-be trade."""
    try:
        capital = float(settings.TOTAL_CAPITAL)
        risk_pct = float(settings.MAX_POSITION_SIZE_PCT)
    except (TypeError, ValueError, AttributeError):
        return 0
    risk_per_share = (stop - entry) if is_short else (entry - stop)
    if risk_per_share <= 0 or entry <= 0 or capital <= 0:
        return 0
    modifier = 1.0
    if is_short:
        try:
            from short_strategies.common.config import get_short_config

            modifier = float(get_short_config().risk_modifier)
        except Exception:  # noqa: BLE001
            modifier = 0.65
    shares = int(capital * risk_pct * modifier / risk_per_share)
    shares = min(shares, int(capital * 0.10 / entry))
    if str(grade or "").upper() == "B":
        shares = int(shares * 0.75)
    return max(0, shares)


# ---------------------------------------------------------------------------
# Card assembly (spec section 8.2 component contract).
# ---------------------------------------------------------------------------


def _data_state(ind: Optional[Dict[str, Any]],
                ai_degraded: bool) -> Dict[str, str]:
    if ind is None:
        return {
            "state": "unavailable",
            "message": "Not enough price history to complete this "
                       "analysis — the reasoning below needs roughly ten "
                       "months of trading data. The card fills in as data "
                       "accumulates.",
        }
    if ai_degraded:
        return {
            "state": "degraded",
            "message": "AI narration unavailable — showing the bot's "
                       "rule-based analysis only. Every number here is "
                       "computed locally and remains accurate.",
        }
    return {"state": "live", "message": ""}


def build_position_card(row: Dict[str, Any],
                        ai_degraded: bool = False) -> Dict[str, Any]:
    """Assemble the fixed card structure for one open position."""
    ind = row.get("indicators")
    levels = row.get("key_levels") or {}
    is_short = str(row.get("side", "long")).lower() == "short"
    state = _data_state(ind, ai_degraded)

    stress = build_stress_test(
        entry=row.get("entry_price"),
        shares=row.get("quantity"),
        stop=row.get("stop_price"),
        target=row.get("target_price"),
        current=row.get("current_price"),
        levels=levels,
        is_short=is_short,
        is_hypothetical=False,
    )

    return {
        "kind": "position",
        "identity": {
            "symbol": row.get("symbol"),
            "price": row.get("current_price"),
            "change_pct": row.get("unrealized_pct"),
            "change_label": "since entry",
            "tag": strategy_tag(row.get("strategy", ""), is_position=True),
            "direction": "short" if is_short else "long",
            "asset_type": _asset_type(row.get("symbol")),
            **_leverage_meta(row.get("symbol")),
        },
        "etf_info": build_etf_info(
            row.get("symbol"), _asset_type(row.get("symbol"))
        ),
        "summary": _position_summary(row, is_short),
        "ratings": build_ratings(row),
        "conditions": build_conditions(ind, is_short),
        "reasoning": build_reasoning(ind, levels, is_short),
        "invalidation": build_invalidation(
            ind, levels, row.get("stop_price"), is_short
        ),
        "stress_test": stress,
        "stress_test_unavailable_reason": (
            None if stress else
            "A stress test appears once the trade has an entry and a "
            "protective stop to anchor the math."
        ),
        "detail": {
            "entry_price": row.get("entry_price"),
            "quantity": row.get("quantity"),
            "stop_price": row.get("stop_price"),
            "target_price": row.get("target_price"),
            "unrealized_pnl": row.get("unrealized_pnl"),
            "unrealized_pct": row.get("unrealized_pct"),
            "r_multiple": row.get("r_progress"),
            "order_status": "Open — managed automatically by the bot",
            "entry_time": row.get("entry_time"),
        },
        "data_state": state["state"],
        "data_state_message": state["message"],
    }


def build_watchlist_card(row: Dict[str, Any], settings,
                         ai_degraded: bool = False) -> Dict[str, Any]:
    """Assemble the fixed card structure for one watchlist symbol."""
    ind = row.get("indicators")
    levels = row.get("key_levels") or {}
    sig = row.get("signal") or {}
    strategy = str(sig.get("strategy") or "")
    is_short = strategy.startswith("short_")
    state = _data_state(ind, ai_degraded)

    stress = None
    sizing_note = ""
    entry = sig.get("entry")
    stop = sig.get("stop")
    if entry and stop:
        shares = hypothetical_shares(
            float(entry), float(stop), sig.get("grade"), is_short, settings
        )
        if shares <= 0:
            sizing_note = (
                "Shown per single share: at the strategy's automatic "
                "sizing rules this trade rounds down to zero shares — "
                "that (not a fault) is why the bot did not place it."
            )
        stress = build_stress_test(
            entry=float(entry),
            shares=shares,
            stop=float(stop),
            target=float(sig["target"]) if sig.get("target") else None,
            current=row.get("price"),
            levels=levels,
            is_short=is_short,
            is_hypothetical=True,
            sizing_note=sizing_note,
        )

    rejection_plain = explain_rejection(row.get("last_rejection"))

    return {
        "kind": "watchlist",
        "identity": {
            "symbol": row.get("symbol"),
            "price": row.get("price"),
            "change_pct": row.get("change_pct"),
            "change_label": "today",
            "tag": (strategy_tag(strategy, is_position=False)
                    if strategy else "Watchlist"),
            "direction": "short" if is_short else "long",
            "asset_type": _asset_type(row.get("symbol")),
            **_leverage_meta(row.get("symbol")),
        },
        "etf_info": build_etf_info(
            row.get("symbol"), _asset_type(row.get("symbol"))
        ),
        "summary": _watchlist_summary(row),
        "status_plain": rejection_plain,
        "ratings": build_ratings(row),
        "conditions": build_conditions(ind, is_short),
        "reasoning": build_reasoning(ind, levels, is_short),
        "invalidation": build_invalidation(ind, levels, stop, is_short),
        "stress_test": stress,
        "stress_test_unavailable_reason": (
            None if stress else
            "No suggested trade yet — a stress test appears when the "
            "strategy proposes an entry and a protective stop."
        ),
        "detail": {
            "entry_price": entry,
            "quantity": (stress or {}).get("shares"),
            "stop_price": stop,
            "target_price": sig.get("target"),
            "unrealized_pnl": None,
            "unrealized_pct": None,
            "r_multiple": None,
            "order_status": "No position open — analysis only",
            "entry_time": None,
        },
        "data_state": state["state"],
        "data_state_message": state["message"],
    }
