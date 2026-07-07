"""
AI commentary engine for the Analyst dashboard (feature TA2).

Generates a "live analyst" payload covering three panels — open positions,
watchlist setups, and market overview — refreshed every few minutes during
market hours.  Deliberately parallel to :mod:`ai.analyst` (same OpenRouter
call shape, tolerant JSON parsing) but a separate module: the veto path must
stay untouched.

Design principle — **the numbers never come from the model.**  Every fact
(prices, indicator readings, distances, R-multiples, gate results, scores) is
computed locally by existing code and passed to the LLM as structured
context; the LLM only turns facts into prose.  A deterministic template
fallback renders the same facts as terse prose when the LLM is unavailable —
the page degrades, never empties.

Fail-open (the opposite of the veto's fail-closed rule, and correct: this
layer influences no order):

* missing key / HTTP error / timeout / parse failure / budget exhaustion
  -> template commentary from the same facts, ``source: "template"``.

Refresh model: request-driven.  ``get_payload()`` serves the cached payload
(``DATA_DIR/ai_commentary.json``) immediately and, when it is older than
``AI_COMMENTARY_INTERVAL_MINUTES`` and the market is open, kicks off one
async refresh in the background.  No browser polling -> no refresh -> no
provider or LLM spend.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx
import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

ET = ZoneInfo("America/New_York")
COMMENTARY_FILE = "ai_commentary.json"

# The 11 SPDR sector ETFs for the rotation heat strip.
SECTOR_ETFS: List[Tuple[str, str]] = [
    ("XLK", "Technology"),
    ("XLF", "Financials"),
    ("XLE", "Energy"),
    ("XLV", "Health Care"),
    ("XLY", "Cons. Discretionary"),
    ("XLP", "Cons. Staples"),
    ("XLI", "Industrials"),
    ("XLB", "Materials"),
    ("XLRE", "Real Estate"),
    ("XLU", "Utilities"),
    ("XLC", "Communications"),
]

# VIX close -> volatility-regime bucket.
_VIX_BUCKETS = [(15.0, "low"), (20.0, "normal"), (30.0, "elevated")]


def vix_bucket(value: float) -> str:
    """Bucket a VIX close into low / normal / elevated / crisis."""
    for ceiling, label in _VIX_BUCKETS:
        if value < ceiling:
            return label
    return "crisis"


def is_market_open(settings, now: Optional[datetime] = None) -> bool:
    """US regular-hours check (weekdays 09:30–16:00 ET; mirrors the engine)."""
    now = now or datetime.now(tz=ET)
    if now.weekday() > 4:
        return False
    open_t = dt_time(settings.MARKET_OPEN_HOUR, settings.MARKET_OPEN_MINUTE)
    close_t = dt_time(settings.MARKET_CLOSE_HOUR, settings.MARKET_CLOSE_MINUTE)
    return open_t <= now.time() < close_t


# ---------------------------------------------------------------------------
# Sentiment + live indicator readings (deterministic — reuses signals/*)
# ---------------------------------------------------------------------------


def compute_indicators(df) -> Optional[Dict[str, Any]]:
    """Current indicator scalars + state booleans from an OHLCV frame.

    Same math as the scan-time scoring engine (:mod:`signals.combined_filter`)
    so the Analyst page never disagrees with the scanner.  Returns ``None``
    when the frame is too short for the EMA-200 stack.
    """
    if df is None or len(df) < 200:
        return None
    try:
        from signals.combined_filter import _compute_atr
        from signals.ema_signals import calculate_ema
        from signals.macd_signals import calculate_macd
        from signals.ripster_cloud import calculate_ripster
        from signals.rsi_signals import calculate_rsi
        from signals.volume_signals import calculate_volume

        rsi = calculate_rsi(df)
        macd = calculate_macd(df)
        ema = calculate_ema(df)
        vol = calculate_volume(df)
        rip = calculate_ripster(df)
        atr = _compute_atr(df)
        price = float(df["Close"].iloc[-1])
        prev_hist = None
        # Histogram direction needs the prior bar; recompute cheaply.
        try:
            close = df["Close"].astype(float)
            macd_line = close.ewm(span=12, adjust=False).mean() - close.ewm(
                span=26, adjust=False
            ).mean()
            signal_line = macd_line.ewm(span=9, adjust=False).mean()
            hist = macd_line - signal_line
            prev_hist = float(hist.iloc[-2])
        except Exception:  # noqa: BLE001 -- direction flag is a nice-to-have
            prev_hist = None

        return {
            "price": round(price, 4),
            "rsi": round(rsi.rsi_value, 1),
            "rsi_rising": rsi.is_rising,
            "rsi_overbought": rsi.is_overbought,
            "rsi_momentum_zone": rsi.is_momentum_zone,
            "macd_hist": round(macd.histogram, 4),
            "macd_hist_expanding": (
                prev_hist is not None
                and macd.histogram > 0
                and macd.histogram > prev_hist
            ),
            "macd_bullish": macd.is_momentum_intact or macd.is_confirmed_bullish,
            "ema9": round(ema.ema9, 4),
            "ema20": round(ema.ema20, 4),
            "ema50": round(ema.ema50, 4),
            "ema200": round(ema.ema200, 4),
            "above_ema20": price > ema.ema20,
            "above_ema50": price > ema.ema50,
            "above_ema200": ema.is_above_ema200,
            "bullish_stack": ema.has_bullish_stack,
            "volume_ratio": round(vol.volume_ratio, 2),
            "obv_confirming": vol.is_obv_confirming,
            "above_vwap": vol.is_above_vwap,
            "fast_cloud_bullish": rip.fast_cloud_bullish,
            "slow_cloud_bullish": rip.slow_cloud_bullish,
            "squeeze_hint": ema.is_compressed,
            "atr14": round(atr, 4),
            "atr_pct": round(atr / price * 100.0, 2) if price > 0 else None,
        }
    except Exception as exc:  # noqa: BLE001 -- indicators are best-effort
        log.warning("commentary.indicators_failed", error=str(exc))
        return None


def derive_sentiment(df, strategy: str = "momentum") -> Optional[Dict[str, Any]]:
    """Bullish / bearish / neutral chip with confidence, from indicator states.

    Reuses the exact ``bullish_score`` functions and strategy weights from the
    scoring engine, so the chip is consistent scan-to-scan and never asked of
    the LLM.
    """
    if df is None or len(df) < 200:
        return None
    try:
        from config.settings import weights_for_strategy
        from signals.ema_signals import bullish_score as ema_score, calculate_ema
        from signals.macd_signals import bullish_score as macd_score, calculate_macd
        from signals.ripster_cloud import (
            bullish_score as rip_score,
            calculate_ripster,
        )
        from signals.rsi_signals import bullish_score as rsi_score, calculate_rsi
        from signals.volume_signals import (
            bullish_score as vol_score,
            calculate_volume,
        )

        strategy = strategy if strategy in (
            "momentum", "vcp_breakout", "pead", "swing", "mean_reversion"
        ) else "momentum"
        w = weights_for_strategy(strategy)
        score = (
            w["rsi"] * rsi_score(calculate_rsi(df), strategy)
            + w["macd"] * macd_score(calculate_macd(df))
            + w["ema"] * ema_score(calculate_ema(df), strategy)
            + w["volume"] * vol_score(calculate_volume(df), strategy)
            + w["ripster"] * rip_score(calculate_ripster(df))
        )
        score = max(0.0, min(1.0, float(score)))
        if score >= 0.55:
            label = "bullish"
        elif score <= 0.40:
            label = "bearish"
        else:
            label = "neutral"
        # Confidence: how far the score sits from the neutral centre (0.475),
        # scaled so a 0.95+ or 0.0 score reads as ~100%.
        confidence = min(1.0, abs(score - 0.475) / 0.475)
        return {
            "label": label,
            "score": round(score, 4),
            "confidence": round(confidence, 2),
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("commentary.sentiment_failed", error=str(exc))
        return None


def key_levels(df, max_per_side: int = 2) -> Dict[str, List[Dict[str, Any]]]:
    """Nearest support/resistance from swing pivots (fractal, 2-bar wings).

    Pivots over the last ~90 bars are clustered within 0.5 x ATR(14); a
    cluster's strength is its touch count.  Returns the top levels below
    (support) and above (resistance) the current price.
    """
    out: Dict[str, List[Dict[str, Any]]] = {"support": [], "resistance": []}
    if df is None or len(df) < 30:
        return out
    try:
        from signals.combined_filter import _compute_atr

        window = df.tail(90)
        highs = window["High"].astype(float).tolist()
        lows = window["Low"].astype(float).tolist()
        price = float(df["Close"].iloc[-1])
        atr = _compute_atr(df) or (price * 0.02)
        tol = 0.5 * atr

        pivots: List[float] = []
        for i in range(2, len(window) - 2):
            if highs[i] == max(highs[i - 2 : i + 3]):
                pivots.append(highs[i])
            if lows[i] == min(lows[i - 2 : i + 3]):
                pivots.append(lows[i])

        clusters: List[List[float]] = []
        for p in sorted(pivots):
            if clusters and p - clusters[-1][-1] <= tol:
                clusters[-1].append(p)
            else:
                clusters.append([p])

        levels = [
            {"price": round(sum(c) / len(c), 2), "touches": len(c)}
            for c in clusters
        ]
        support = [l for l in levels if l["price"] < price]
        resistance = [l for l in levels if l["price"] >= price]
        support.sort(key=lambda l: (-l["touches"], price - l["price"]))
        resistance.sort(key=lambda l: (-l["touches"], l["price"] - price))
        out["support"] = sorted(
            support[:max_per_side], key=lambda l: -l["price"]
        )
        out["resistance"] = sorted(
            resistance[:max_per_side], key=lambda l: l["price"]
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("commentary.levels_failed", error=str(exc))
    return out


# ---------------------------------------------------------------------------
# Deterministic template commentary (the fail-open fallback)
# ---------------------------------------------------------------------------


def template_position_commentary(ind: Optional[Dict[str, Any]]) -> str:
    """Terse indicator narration from computed facts — no LLM, no numbers
    the payload doesn't already carry."""
    if not ind:
        return "Indicator data unavailable — showing position risk math only."
    parts: List[str] = []
    rsi = ind.get("rsi")
    if rsi is not None:
        zone = (
            "overbought" if ind.get("rsi_overbought")
            else "in the momentum zone" if ind.get("rsi_momentum_zone")
            else "above neutral" if rsi >= 50 else "below neutral"
        )
        trend = "rising" if ind.get("rsi_rising") else "easing"
        parts.append(f"RSI at {rsi:.0f}, {zone} and {trend}.")
    if ind.get("ema20") is not None:
        rel = "above" if ind.get("above_ema20") else "below"
        parts.append(f"Price {rel} the 20 EMA (${ind['ema20']:.2f}).")
    hist = ind.get("macd_hist")
    if hist is not None:
        sign = "positive" if hist > 0 else "negative"
        shape = " and expanding" if ind.get("macd_hist_expanding") else ""
        parts.append(f"MACD histogram {sign}{shape}.")
    vr = ind.get("volume_ratio")
    if vr is not None:
        parts.append(f"Volume {vr:.1f}x its 20-day average.")
    clouds = []
    if ind.get("fast_cloud_bullish"):
        clouds.append("fast")
    if ind.get("slow_cloud_bullish"):
        clouds.append("slow")
    if clouds:
        parts.append(f"Ripster {' and '.join(clouds)} cloud"
                     f"{'s' if len(clouds) > 1 else ''} green.")
    else:
        parts.append("Both Ripster clouds red.")
    return " ".join(parts)


def template_position_action(row: Dict[str, Any],
                             sentiment: Optional[Dict[str, Any]]) -> str:
    """One advisory sentence from the position's computed risk math."""
    r = row.get("r_progress")
    d_target = row.get("distance_to_target_pct")
    d_stop = row.get("distance_to_stop_pct")
    if d_target is not None and d_target <= 1.5:
        return "Approaching target — watch for an exit or partial take."
    if d_stop is not None and d_stop <= 1.5:
        return "Price is close to the stop — the setup is on its last line."
    if r is not None and r >= 1.0:
        return "Consider tightening the stop toward breakeven (>= 1R gained)."
    if sentiment and sentiment.get("label") == "bearish":
        return "Setup weakening — watch for a reversal or a stop-out."
    return "Hold — levels intact, no action suggested."


def template_watchlist_commentary(row: Dict[str, Any],
                                  ind: Optional[Dict[str, Any]]) -> str:
    """Setup summary for a watchlist symbol from monitor + indicator facts."""
    parts: List[str] = []
    status = row.get("status", "idle")
    sig = row.get("signal")
    rej = row.get("last_rejection")
    if status == "held":
        parts.append("Already in the book.")
    elif status == "signal" and sig:
        parts.append(
            f"Live {sig.get('strategy', '')} signal, grade {sig.get('grade', '?')}"
            f" — proposed entry ${sig.get('entry')}, stop ${sig.get('stop')},"
            f" target ${sig.get('target')}."
        )
    elif status == "near_entry":
        parts.append("Setup exists but price drifted from the signal "
                     "(freshness gate) — re-arms on the next scan.")
    elif status == "rejected" and rej:
        parts.append(
            f"Last scan rejected at `{rej.get('gate')}`: {rej.get('detail')}"
        )
    elif status == "excluded":
        parts.append("Excluded by trade selection — not being scanned.")
    else:
        parts.append("No recent signal; waiting for a setup.")
    if ind:
        rel20 = "above" if ind.get("above_ema20") else "below"
        rsi = ind.get("rsi")
        parts.append(
            f"RSI {rsi:.0f}, price {rel20} the 20 EMA"
            f" (${ind['ema20']:.2f})." if rsi is not None else ""
        )
        if ind.get("squeeze_hint"):
            parts.append("EMAs compressed — potential squeeze forming.")
    return " ".join(p for p in parts if p)


def template_market_summary(market: Dict[str, Any]) -> str:
    """Market-overview prose from computed regime / VIX / sector facts."""
    parts: List[str] = []
    spy = market.get("spy") or {}
    if spy.get("regime"):
        parts.append(
            f"SPY is in a {spy['regime']} regime"
            f" ({spy.get('volatility', 'normal')} volatility)."
        )
    qqq = market.get("qqq") or {}
    if qqq.get("regime") and qqq.get("regime") != spy.get("regime"):
        parts.append(f"QQQ diverges: {qqq['regime']} regime.")
    vix = market.get("vix") or {}
    if vix.get("value") is not None:
        parts.append(f"VIX {vix['value']:.1f} — {vix.get('bucket')} "
                     "volatility regime.")
    sectors = market.get("sectors") or []
    ranked = [s for s in sectors if s.get("change_1d") is not None]
    if ranked:
        ranked.sort(key=lambda s: s["change_1d"], reverse=True)
        parts.append(
            f"Sector breadth favors {ranked[0]['name']}"
            f" ({ranked[0]['change_1d']:+.1f}%);"
            f" {ranked[-1]['name']} lagging ({ranked[-1]['change_1d']:+.1f}%)."
        )
    bias = market.get("bias") or {}
    if bias.get("note"):
        parts.append(bias["note"])
    return " ".join(parts) or "Market data unavailable."


def derive_market_bias(market: Dict[str, Any]) -> Dict[str, Any]:
    """Overall bias chip + what it means for the strategy mix (deterministic)."""
    spy = market.get("spy") or {}
    vix = market.get("vix") or {}
    regime = spy.get("regime", "sideways")
    bucket = vix.get("bucket") or (
        "elevated" if spy.get("volatility") == "high" else "normal"
    )
    mult = (spy.get("weight_multipliers") or {})
    if regime == "bull" and bucket in ("low", "normal"):
        label, note = "risk-on", (
            "Conditions favor long momentum setups — momentum family weight "
            f"x{mult.get('momentum', 1.0)}."
        )
    elif regime == "bear" or bucket == "crisis":
        label, note = "risk-off", (
            "Defensive conditions — momentum de-weighted "
            f"(x{mult.get('momentum', 1.0)}), mean-reversion favored "
            f"(x{mult.get('swing', 1.0)})."
        )
    else:
        label, note = "mixed", (
            "Mixed conditions — selective entries; swing family weighted "
            f"x{mult.get('swing', 1.0)}."
        )
    return {"label": label, "note": note}


# ---------------------------------------------------------------------------
# Facts builders (synchronous; run in a threadpool by the refresh loop)
# ---------------------------------------------------------------------------


def _load_positions(data_dir: Path) -> List[Dict[str, Any]]:
    path = data_dir / "open_positions.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return list(data.values()) if isinstance(data, dict) else []


def _fetch_bars(symbol: str, settings):
    """Daily bars through the shared TTL cache (same period as the engine's
    scan, so a symbol scanned this hour costs zero provider calls)."""
    from data.fetcher import fetch_ohlcv

    return fetch_ohlcv(symbol, period=settings.OHLCV_FETCH_PERIOD)


def build_position_facts(settings) -> List[Dict[str, Any]]:
    """Per-position card facts: live risk math + indicators + sentiment."""
    from dashboard import quotes
    from dashboard.live_router import _position_row

    data_dir = Path(settings.DATA_DIR)
    positions = _load_positions(data_dir)
    symbols = [str(p.get("symbol", "")) for p in positions if p.get("symbol")]
    quote_map = quotes.get_quotes(symbols) if symbols else {}

    rows: List[Dict[str, Any]] = []
    for pos in positions:
        sym = str(pos.get("symbol", ""))
        row = _position_row(pos, quote_map.get(sym, {}), settings)
        df = None
        try:
            df = _fetch_bars(sym, settings)
        except Exception:  # noqa: BLE001 -- a bad symbol never fails the panel
            df = None
        ind = compute_indicators(df)
        row["indicators"] = ind
        row["sentiment"] = derive_sentiment(df, str(pos.get("strategy", "")))
        row["key_levels"] = key_levels(df)
        rows.append(row)
    rows.sort(key=lambda r: r["symbol"])
    return rows


def build_watchlist_facts(settings) -> List[Dict[str, Any]]:
    """Watchlist cards: monitor status + readiness + levels (+ indicators for
    the top-N symbols by status rank)."""
    from dashboard.watchlist_router import watchlist_monitor

    # watchlist_monitor is an async endpoint; run it on a private loop since
    # this builder itself runs inside a worker thread (never the app's loop).
    payload = asyncio.run(watchlist_monitor(_user="commentary"))

    rows = list(payload.get("symbols", []))
    limit = int(getattr(settings, "AI_COMMENTARY_WATCHLIST_LIMIT", 10))
    # Held symbols already have a card in the positions panel.
    rows = [r for r in rows if r.get("status") != "held"]

    enriched: List[Dict[str, Any]] = []
    for i, row in enumerate(rows):
        card = dict(row)
        ind = None
        sentiment = None
        levels: Dict[str, Any] = {"support": [], "resistance": []}
        if i < limit:
            try:
                df = _fetch_bars(str(row.get("symbol", "")), settings)
            except Exception:  # noqa: BLE001
                df = None
            ind = compute_indicators(df)
            strategy = (row.get("signal") or {}).get("strategy") or "momentum"
            sentiment = derive_sentiment(df, str(strategy))
            levels = key_levels(df)
        card["indicators"] = ind
        card["sentiment"] = sentiment
        card["key_levels"] = levels
        card["readiness"] = _readiness(row, sentiment)
        enriched.append(card)
    enriched.sort(key=lambda c: -(c.get("readiness") or 0))
    return enriched


def _readiness(row: Dict[str, Any],
               sentiment: Optional[Dict[str, Any]]) -> int:
    """Entry-readiness 0–100: the last scan's combined score when a signal
    exists, otherwise the sentiment score, otherwise a status heuristic."""
    sig = row.get("signal")
    if sig and sig.get("strength") is not None:
        try:
            return int(round(float(sig["strength"]) * 100))
        except (TypeError, ValueError):
            pass
    if sentiment and sentiment.get("score") is not None:
        return int(round(float(sentiment["score"]) * 100))
    return {"near_entry": 60, "rejected": 35, "idle": 20, "excluded": 5}.get(
        str(row.get("status", "idle")), 20
    )


def build_market_facts(settings) -> Dict[str, Any]:
    """SPY/QQQ regime, VIX bucket, and the 11-sector heat strip."""
    from analytics.regime import current_regime, detect_regime
    from dashboard import quotes

    market: Dict[str, Any] = {}

    # SPY / QQQ — the same pure detector the engine uses, over bars fetched
    # at the engine's look-back period (the default 6mo window is too short
    # for the 200-day MA and would report "insufficient history").
    for sym, key in (("SPY", "spy"), ("QQQ", "qqq")):
        try:
            df = _fetch_bars(sym, settings)
            market[key] = (
                detect_regime(df, settings).to_dict() if df is not None else {}
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("commentary.regime_failed", symbol=sym, error=str(exc))
            market[key] = {}
    if not market.get("spy"):
        # Fall back to the shared benchmark path (also neutral-on-error).
        try:
            market["spy"] = current_regime(settings).to_dict()
        except Exception:  # noqa: BLE001
            market["spy"] = {}

    # Benchmark quotes for the header chips.
    try:
        bench = quotes.get_quotes(["SPY", "QQQ"], include_prev_close=True)
        for sym in ("SPY", "QQQ"):
            q = bench.get(sym) or {}
            market.setdefault(sym.lower(), {})
            market[sym.lower()]["last_price"] = q.get("price")
            market[sym.lower()]["change_pct"] = q.get("change_pct")
    except Exception:  # noqa: BLE001
        pass

    # VIX regime — ^VIX daily close bucketed; realized-vol proxy as fallback.
    vix: Dict[str, Any] = {"value": None, "bucket": None, "source": "vix"}
    try:
        vix_df = _fetch_bars("^VIX", settings)
        if vix_df is not None and len(vix_df) > 0 and "Close" in vix_df:
            value = float(vix_df["Close"].iloc[-1])
            vix = {"value": round(value, 2), "bucket": vix_bucket(value),
                   "source": "vix"}
    except Exception as exc:  # noqa: BLE001
        log.warning("commentary.vix_failed", error=str(exc))
    if vix["value"] is None:
        # Labeled fallback: the SPY realized-vol flag from regime detection.
        realized = market.get("spy", {}).get("volatility")
        if realized:
            vix = {
                "value": None,
                "bucket": "elevated" if realized == "high" else "normal",
                "source": "realized_vol",
            }
    market["vix"] = vix

    # Sector heat strip: 1-day change from the batch quote service, 5-day
    # change from cached daily bars.
    sectors: List[Dict[str, Any]] = []
    try:
        etf_symbols = [s for s, _ in SECTOR_ETFS]
        quote_map = quotes.get_quotes(etf_symbols, include_prev_close=True)
        for sym, name in SECTOR_ETFS:
            q = quote_map.get(sym) or {}
            change_5d = None
            try:
                df = _fetch_bars(sym, settings)
                if df is not None and len(df) >= 6 and "Close" in df:
                    now_c = float(df["Close"].iloc[-1])
                    then_c = float(df["Close"].iloc[-6])
                    if then_c > 0:
                        change_5d = round((now_c - then_c) / then_c * 100.0, 2)
            except Exception:  # noqa: BLE001
                change_5d = None
            sectors.append({
                "symbol": sym,
                "name": name,
                "price": q.get("price"),
                "change_1d": q.get("change_pct"),
                "change_5d": change_5d,
            })
    except Exception as exc:  # noqa: BLE001
        log.warning("commentary.sectors_failed", error=str(exc))
    market["sectors"] = sectors

    market["bias"] = derive_market_bias(market)
    return market


# ---------------------------------------------------------------------------
# LLM prose generation (batched — one call per panel, budget-capped)
# ---------------------------------------------------------------------------

_COMMENTATOR_SYSTEM = (
    "You are a concise technical-analysis commentator for an automated "
    "long-only equity trading dashboard. You are given COMPUTED FACTS as "
    "JSON. Narrate them in plain English. NEVER invent numbers, prices, or "
    "news — only restate the numbers provided. This is display-only "
    "commentary, not financial advice, and it influences no order. Respond "
    "with STRICT JSON only, no prose outside the JSON."
)


def _positions_prompt(rows: List[Dict[str, Any]]) -> str:
    facts = [
        {
            "symbol": r["symbol"],
            "strategy": r.get("strategy"),
            "entry": r.get("entry_price"),
            "stop": r.get("stop_price"),
            "target": r.get("target_price"),
            "current": r.get("current_price"),
            "unrealized_pct": r.get("unrealized_pct"),
            "r_progress": r.get("r_progress"),
            "distance_to_stop_pct": r.get("distance_to_stop_pct"),
            "distance_to_target_pct": r.get("distance_to_target_pct"),
            "sentiment": r.get("sentiment"),
            "indicators": r.get("indicators"),
            "key_levels": r.get("key_levels"),
        }
        for r in rows
    ]
    return (
        "For EACH open position below, write 2-4 sentences of technical "
        "commentary (current technical state, momentum, notable levels) and "
        "ONE suggested-action sentence chosen from these themes: hold / "
        "consider tightening stop toward breakeven / approaching target, "
        "watch for exit / setup weakening, watch for reversal.\n"
        'Return: {"positions": [{"symbol": "...", "commentary": "...", '
        '"action": "..."}]}\n\nFACTS:\n' + json.dumps(facts)
    )


def _watchlist_prompt(rows: List[Dict[str, Any]]) -> str:
    facts = [
        {
            "symbol": r["symbol"],
            "status": r.get("status"),
            "price": r.get("price"),
            "change_pct": r.get("change_pct"),
            "readiness": r.get("readiness"),
            "signal": r.get("signal"),
            "last_rejection": r.get("last_rejection"),
            "sentiment": r.get("sentiment"),
            "indicators": r.get("indicators"),
            "key_levels": r.get("key_levels"),
        }
        for r in rows
    ]
    return (
        "For EACH watchlist symbol below, write 1-3 sentences summarizing "
        "the current technical setup: what the setup is, what would trigger "
        "an entry, and the key level to watch. Mention the failing gate when "
        "the last scan rejected it.\n"
        'Return: {"watchlist": [{"symbol": "...", "commentary": "..."}]}\n\n'
        "FACTS:\n" + json.dumps(facts)
    )


def _market_prompt(market: Dict[str, Any]) -> str:
    facts = {k: market.get(k) for k in ("spy", "qqq", "vix", "sectors", "bias")}
    return (
        "Write 2-3 sentences synthesizing these market conditions: benchmark "
        "trend/regime, volatility regime, sector breadth, and what the "
        "computed bias means for a long-only momentum/swing strategy mix.\n"
        'Return: {"summary": "..."}\n\nFACTS:\n' + json.dumps(facts)
    )


def parse_llm_json(content: str) -> Optional[Dict[str, Any]]:
    """Extract the first JSON object from a model response (tolerant of code
    fences / surrounding prose, like :func:`ai.analyst._parse_verdict`)."""
    text = (content or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


class CommentaryEngine:
    """Builds and refreshes the Analyst payload.

    One instance lives per dashboard process (module-level singleton via
    :func:`get_engine`).  All state that must survive a restart — the payload
    itself and the daily LLM budget counter — is persisted inside
    ``DATA_DIR/ai_commentary.json`` with atomic writes.
    """

    def __init__(self, settings) -> None:
        self._settings = settings
        self._data_dir = Path(settings.DATA_DIR)
        self._path = self._data_dir / COMMENTARY_FILE
        self._lock = threading.Lock()
        self._refreshing = False
        self._last_poll_monotonic: float = 0.0
        self._last_error: Optional[str] = None
        self._last_run_at: Optional[str] = None
        self._payload: Optional[Dict[str, Any]] = self._load()
        # Input-hash short-circuit state: {section: (facts_hash, prose)}
        self._section_hashes: Dict[str, str] = {}

    # ----------------------------------------------------------- persistence

    def _load(self) -> Optional[Dict[str, Any]]:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, OSError):
            return None

    def _save(self, payload: Dict[str, Any]) -> None:
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(self._data_dir), prefix=".ai_commentary_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, default=str)
                os.replace(tmp, str(self._path))
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            log.error("commentary.save_failed", error=str(exc))

    # ---------------------------------------------------------------- budget

    def _budget(self) -> Dict[str, Any]:
        """Today's LLM budget state from the persisted payload."""
        today = datetime.now(tz=ET).date().isoformat()
        budget = (self._payload or {}).get("budget") or {}
        used = int(budget.get("used", 0)) if budget.get("date") == today else 0
        return {
            "date": today,
            "used": used,
            "max": int(self._settings.AI_COMMENTARY_MAX_CALLS_PER_DAY),
        }

    def budget_exhausted(self) -> bool:
        b = self._budget()
        return b["used"] >= b["max"]

    # ---------------------------------------------------------------- status

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self._settings.AI_COMMENTARY_ENABLED),
            "model": self._settings.AI_COMMENTARY_MODEL,
            "interval_minutes": int(self._settings.AI_COMMENTARY_INTERVAL_MINUTES),
            "market_open": is_market_open(self._settings),
            "refreshing": self._refreshing,
            "last_run_at": self._last_run_at
            or (self._payload or {}).get("generated_at"),
            "last_error": self._last_error,
            "budget": self._budget(),
            "has_payload": self._payload is not None,
        }

    # ----------------------------------------------------------- poll + serve

    def note_poll(self) -> None:
        self._last_poll_monotonic = time.monotonic()

    def _recently_polled(self) -> bool:
        idle = float(
            getattr(self._settings, "AI_COMMENTARY_IDLE_SUPPRESS_MINUTES", 15)
        )
        return (time.monotonic() - self._last_poll_monotonic) <= idle * 60.0

    def _age_seconds(self) -> Optional[float]:
        gen = (self._payload or {}).get("generated_at")
        if not gen:
            return None
        try:
            ts = datetime.fromisoformat(str(gen))
        except ValueError:
            return None
        now = datetime.now(tz=ts.tzinfo) if ts.tzinfo else datetime.now()
        return max(0.0, (now - ts).total_seconds())

    def is_stale(self) -> bool:
        age = self._age_seconds()
        interval = float(self._settings.AI_COMMENTARY_INTERVAL_MINUTES) * 60.0
        return age is None or age > interval

    def should_refresh(self) -> bool:
        """Scheduled-refresh gate: stale payload AND (market open OR first
        ever run) AND someone is actually looking at the page."""
        if self._refreshing:
            return False
        if not self.is_stale():
            return False
        if not self._recently_polled():
            return False
        if self._payload is None:
            return True  # first run may happen off-hours so the page isn't blank
        return is_market_open(self._settings)

    def payload_for_client(self) -> Dict[str, Any]:
        """The whole payload, annotated with freshness + status for the UI."""
        payload = dict(self._payload or {
            "generated_at": None,
            "positions": [],
            "watchlist": [],
            "market": {},
        })
        interval = int(self._settings.AI_COMMENTARY_INTERVAL_MINUTES)
        payload["interval_minutes"] = interval
        payload["market_open"] = is_market_open(self._settings)
        payload["stale"] = self.is_stale()
        payload["refreshing"] = self._refreshing
        payload["budget"] = self._budget()
        payload["last_error"] = self._last_error
        gen = payload.get("generated_at")
        next_at = None
        if gen:
            try:
                next_at = (
                    datetime.fromisoformat(str(gen))
                    + timedelta(minutes=interval)
                ).isoformat()
            except ValueError:
                next_at = None
        payload["next_refresh_at"] = next_at
        return payload

    # ---------------------------------------------------------------- refresh

    async def refresh(self, force: bool = False) -> Dict[str, Any]:
        """Rebuild facts and (budget permitting) LLM prose; persist + return.

        Never raises: every failure path degrades to template prose and is
        recorded in ``last_error``.
        """
        if self._refreshing and not force:
            return self.payload_for_client()
        self._refreshing = True
        try:
            payload = await self._do_refresh()
            self._payload = payload
            self._save(payload)
            self._last_run_at = payload.get("generated_at")
            return self.payload_for_client()
        except Exception as exc:  # noqa: BLE001 -- fail-open, never break the page
            self._last_error = f"refresh failed: {exc}"
            log.error("commentary.refresh_failed", error=str(exc))
            return self.payload_for_client()
        finally:
            self._refreshing = False

    async def _do_refresh(self) -> Dict[str, Any]:
        from starlette.concurrency import run_in_threadpool

        settings = self._settings
        self._last_error = None

        positions = await run_in_threadpool(build_position_facts, settings)
        watchlist = await run_in_threadpool(build_watchlist_facts, settings)
        market = await run_in_threadpool(build_market_facts, settings)

        budget = self._budget()
        llm_ok = (
            bool(settings.AI_COMMENTARY_ENABLED)
            and bool(settings.OPENROUTER_API_KEY)
            and budget["used"] < budget["max"]
        )

        llm_limit = int(getattr(settings, "AI_COMMENTARY_WATCHLIST_LIMIT", 10))
        wl_for_llm = watchlist[:llm_limit]

        # One batched call per panel, each independently fail-open, each
        # skipped when its input facts are unchanged since the last run.
        pos_prose = await self._panel_prose(
            "positions", positions, _positions_prompt, llm_ok
        ) if positions else {}
        wl_prose = await self._panel_prose(
            "watchlist", wl_for_llm, _watchlist_prompt, llm_ok
        ) if wl_for_llm else {}
        market_summary, market_source = await self._market_prose(market, llm_ok)

        for row in positions:
            prose = pos_prose.get(row["symbol"]) if pos_prose else None
            if prose:
                row["commentary"] = prose.get("commentary") or ""
                row["action"] = prose.get("action") or ""
                row["source"] = "llm"
            if not prose or not row.get("commentary"):
                row["commentary"] = template_position_commentary(
                    row.get("indicators")
                )
                row["action"] = template_position_action(
                    row, row.get("sentiment")
                )
                row["source"] = "template"

        for row in watchlist:
            prose = wl_prose.get(row["symbol"]) if wl_prose else None
            if prose and prose.get("commentary"):
                row["commentary"] = prose["commentary"]
                row["source"] = "llm"
            else:
                row["commentary"] = template_watchlist_commentary(
                    row, row.get("indicators")
                )
                row["source"] = "template"

        market["summary"] = market_summary
        market["source"] = market_source

        return {
            "generated_at": datetime.now(tz=ET).isoformat(),
            "market_open": is_market_open(settings),
            "budget": self._budget(),
            "positions": positions,
            "watchlist": watchlist,
            "market": market,
        }

    # ------------------------------------------------------------- LLM calls

    async def _panel_prose(
        self,
        section: str,
        rows: List[Dict[str, Any]],
        prompt_fn,
        llm_ok: bool,
    ) -> Dict[str, Dict[str, Any]]:
        """One batched LLM call for a panel -> {symbol: {commentary, action}}.

        Returns ``{}`` on any failure (callers fall back to templates).
        Input-hash short-circuit: when the panel's facts are byte-identical to
        the previous generation, reuse the previous prose without a call.
        """
        prompt = prompt_fn(rows)
        facts_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        prev = self._payload or {}
        if (
            self._section_hashes.get(section) == facts_hash
            and prev.get(section)
        ):
            reused = {
                r["symbol"]: {
                    "commentary": r.get("commentary"),
                    "action": r.get("action"),
                }
                for r in prev.get(section, [])
                if r.get("source") == "llm" and r.get("commentary")
            }
            if reused:
                log.info("commentary.hash_skip", section=section)
                return reused
        if not llm_ok or self.budget_exhausted():
            return {}
        content = await self._call_openrouter(prompt)
        if content is None:
            return {}
        obj = parse_llm_json(content)
        if not obj:
            self._last_error = f"{section}: unparseable LLM response"
            return {}
        items = obj.get(section) or obj.get("positions") or obj.get("watchlist")
        if not isinstance(items, list):
            self._last_error = f"{section}: LLM JSON missing '{section}' array"
            return {}
        self._section_hashes[section] = facts_hash
        out: Dict[str, Dict[str, Any]] = {}
        for item in items:
            if isinstance(item, dict) and item.get("symbol"):
                out[str(item["symbol"]).upper()] = {
                    "commentary": str(item.get("commentary", "")).strip(),
                    "action": str(item.get("action", "")).strip(),
                }
        return out

    async def _market_prose(
        self, market: Dict[str, Any], llm_ok: bool
    ) -> Tuple[str, str]:
        prompt = _market_prompt(market)
        facts_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        prev_market = (self._payload or {}).get("market") or {}
        if (
            self._section_hashes.get("market") == facts_hash
            and prev_market.get("source") == "llm"
            and prev_market.get("summary")
        ):
            log.info("commentary.hash_skip", section="market")
            return prev_market["summary"], "llm"
        if llm_ok and not self.budget_exhausted():
            content = await self._call_openrouter(prompt)
            obj = parse_llm_json(content) if content else None
            summary = str((obj or {}).get("summary", "")).strip()
            if summary:
                self._section_hashes["market"] = facts_hash
                return summary, "llm"
        return template_market_summary(market), "template"

    async def _call_openrouter(self, user_prompt: str) -> Optional[str]:
        """Single OpenRouter chat call; counts against the daily budget even
        on failure (a failed request still hit the free-tier cap)."""
        settings = self._settings
        self._bump_budget()
        payload = {
            "model": settings.AI_COMMENTARY_MODEL,
            "messages": [
                {"role": "system", "content": _COMMENTATOR_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 1200,
        }
        headers = {
            "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/r2st/USTradingBot",
            "X-Title": "USTradingBot",
        }
        try:
            async with httpx.AsyncClient(
                timeout=settings.OPENROUTER_TIMEOUT_SECONDS
            ) as client:
                resp = await client.post(
                    f"{settings.OPENROUTER_BASE_URL}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
            return (
                data.get("choices", [{}])[0].get("message", {}).get("content", "")
            ) or None
        except Exception as exc:  # noqa: BLE001 -- fail-open
            self._last_error = f"OpenRouter call failed: {exc}"
            log.warning("commentary.llm_failed", error=str(exc))
            return None

    def _bump_budget(self) -> None:
        """Increment the persisted daily call counter (rolls over at ET
        midnight)."""
        with self._lock:
            budget = self._budget()
            budget["used"] += 1
            if self._payload is None:
                self._payload = {"budget": budget}
            else:
                self._payload["budget"] = budget


# ---------------------------------------------------------------------------
# Module-level singleton (one engine per dashboard process)
# ---------------------------------------------------------------------------

_engine: Optional[CommentaryEngine] = None
_engine_lock = threading.Lock()


def get_engine() -> CommentaryEngine:
    """Return the process-wide :class:`CommentaryEngine` (built lazily)."""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from dashboard.auth import get_settings

                _engine = CommentaryEngine(get_settings())
    return _engine


def reset_engine() -> None:
    """Drop the singleton (tests / settings changes)."""
    global _engine
    with _engine_lock:
        _engine = None
