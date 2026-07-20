"""
Technical-analysis chart API (TA1).

Serves the per-trade TA workstation view: candlesticks with the full
indicator series the system saw at entry, support/resistance levels, and a
deterministic plain-English explanation panel.

* ``GET /api/trade/{symbol}/ta-chart`` — one self-contained payload for the
  chart + explanation.  Trades placed after TA1 shipped render from the
  persisted v2 snapshot in ``trade_rationale.jsonl`` (``source:
  "snapshot"``, zero provider calls); older trades and the current view of
  open positions are recomputed on demand from cached bars (``source:
  "recomputed"`` — the UI captions these "reconstructed from current data").
* ``GET /api/trade/{symbol}/indicators`` — current live indicator scalars
  for open positions (per-symbol TTL cache).

All recomputation goes through the existing ``fetch_ohlcv`` TTL cache, so
an expanded chart for a symbol the engine scanned recently costs zero
provider calls.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import structlog
from fastapi import APIRouter, Depends, HTTPException

from dashboard.auth import get_settings, require_auth
from dashboard.ta_explain import build_explanation
from journal.rationale import _STRATEGY_PATTERN, find_rationale

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/trade", tags=["Signals & TA"])

#: TTL for recomputed chart payloads and live-indicator scalars (seconds).
#: Daily bars only change once per trading day; 5 minutes keeps the modal
#: snappy without ever hammering providers.
CHART_CACHE_TTL = 300.0
INDICATOR_CACHE_TTL = 60.0

_cache_lock = threading.Lock()
_chart_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_indicator_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _cache_get(cache: Dict[str, Tuple[float, Dict[str, Any]]], key: str):
    with _cache_lock:
        hit = cache.get(key)
        if hit is not None and time.monotonic() < hit[0]:
            return hit[1]
    return None


def _cache_put(
    cache: Dict[str, Tuple[float, Dict[str, Any]]],
    key: str,
    value: Dict[str, Any],
    ttl: float,
) -> None:
    with _cache_lock:
        if len(cache) > 256:  # bound memory; these are tiny dicts anyway
            cache.clear()
        cache[key] = (time.monotonic() + ttl, value)


def _load_open_position(data_dir: Path, symbol: str) -> Optional[Dict[str, Any]]:
    path = data_dir / "open_positions.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    positions = data.values() if isinstance(data, dict) else data
    for pos in positions or []:
        if isinstance(pos, dict) and str(pos.get("symbol", "")).upper() == symbol:
            return pos
    return None


def _completed_trade(
    data_dir: Path, symbol: str, entry_time: str
) -> Optional[Dict[str, Any]]:
    """Best-matching completed trade from trades.csv (newest first)."""
    try:
        from analytics.performance import load_completed_trades

        df = load_completed_trades(data_dir / "trades.csv")
        if df.empty:
            return None
        rows = df[df["symbol"].astype(str).str.upper() == symbol]
        if rows.empty:
            return None
        if entry_time and "entry_time" in rows.columns:
            match = rows[
                rows["entry_time"].astype(str).str.startswith(entry_time[:10])
            ]
            if not match.empty:
                rows = match
        return rows.iloc[-1].to_dict()
    except Exception:  # noqa: BLE001 -- journal read is best-effort
        log.debug("ta.completed_trade_failed", symbol=symbol, exc_info=True)
        return None


def _trade_info(
    rec: Optional[Dict[str, Any]],
    pos: Optional[Dict[str, Any]],
    hist: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Merge trade facts from rationale record, open position, and journal.

    Preference order per field: open position (live truth for stops moved
    by ladders) > rationale record (entry-time truth) > completed trade.
    """
    if rec is None and pos is None and hist is None:
        return None

    def pick(*values: Any) -> Any:
        for v in values:
            if v not in (None, "", 0, 0.0):
                return v
        return None

    rec = rec or {}
    pos = pos or {}
    hist = hist or {}
    trade: Dict[str, Any] = {
        "entry_price": pick(
            pos.get("entry_price"), rec.get("entry_price"),
            hist.get("entry_fill_price"),
        ),
        "stop_price": pick(
            pos.get("stop_price"), rec.get("stop_price"), hist.get("stop_price")
        ),
        "target_price": pick(
            pos.get("target_price"), rec.get("target_price"),
            hist.get("target_price"),
        ),
        "quantity": pick(
            pos.get("quantity"), rec.get("quantity"), hist.get("quantity")
        ),
        "grade": pick(pos.get("grade"), rec.get("grade"), hist.get("grade")),
        "strategy": pick(
            pos.get("strategy"), rec.get("strategy"), hist.get("strategy")
        ),
        "direction": pick(
            pos.get("direction"), rec.get("direction"), hist.get("direction")
        ) or "long",
        "entry_time": pick(
            pos.get("entry_time"), rec.get("entry_time"), hist.get("entry_time")
        ),
        "exit_time": hist.get("exit_time") if not pos else None,
        "open": bool(pos),
        "signal_strength": hist.get("signal_strength"),
        "overall_score": rec.get("overall_score"),
        "rsi_value": pick(hist.get("rsi_value")),
        "macd_histogram": pick(hist.get("macd_histogram")),
        "volume_ratio": pick(hist.get("volume_ratio")),
        "levels": pos.get("levels") or [],
    }
    for key in ("entry_price", "stop_price", "target_price"):
        try:
            trade[key] = round(float(trade[key]), 4) if trade[key] else None
        except (TypeError, ValueError):
            trade[key] = None
    return trade


def _recompute_snapshot(symbol: str, settings) -> Optional[Dict[str, Any]]:
    """Recompute the full snapshot from (TTL-cached) bars."""
    from data.fetcher import fetch_ohlcv
    from signals.indicator_snapshot import build_indicator_snapshot

    df = fetch_ohlcv(symbol, period=settings.OHLCV_FETCH_PERIOD)
    if df is None or df.empty:
        return None
    return build_indicator_snapshot(df)


def _entry_bar_index(bars: List[Dict[str, Any]], entry_time: Any) -> Optional[int]:
    """Index of the bar the entry happened on (last bar <= entry date)."""
    if not bars:
        return None
    date = str(entry_time or "")[:10]
    if not date:
        return len(bars) - 1
    idx = None
    for i, bar in enumerate(bars):
        if str(bar.get("t", ""))[:10] <= date:
            idx = i
    return idx if idx is not None else len(bars) - 1


def _live_indicator_payload(symbol: str, settings) -> Dict[str, Any]:
    """Current indicator scalars for one symbol (cached)."""
    cached = _cache_get(_indicator_cache, symbol)
    if cached is not None:
        return cached

    from datetime import datetime, timezone

    snap = _recompute_snapshot(symbol, settings)
    if snap is None:
        raise HTTPException(
            status_code=404, detail=f"No OHLCV data available for {symbol}."
        )
    series = snap.get("series", {})

    def last(name: str) -> Optional[float]:
        values = series.get(name) or []
        return values[-1] if values else None

    state = snap.get("state", {})
    payload: Dict[str, Any] = {
        "symbol": symbol,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "price": state.get("price") or (snap.get("bars") or [{}])[-1].get("c"),
        "rsi": state.get("rsi_value", last("rsi")),
        "macd": last("macd"),
        "macd_signal": last("macd_signal"),
        "macd_hist": last("macd_hist"),
        "ema9": last("ema9"),
        "ema20": last("ema20"),
        "ema50": last("ema50"),
        "ema200": last("ema200"),
        "bb_up": last("bb_up"),
        "bb_mid": last("bb_mid"),
        "bb_lo": last("bb_lo"),
        "obv": last("obv"),
        "atr14": snap.get("atr14"),
        "pct_above_ema20": state.get("pct_above_ema20"),
        "volume_ratio": state.get("volume_ratio"),
        "obv_confirming": state.get("obv_confirming"),
        "above_vwap": state.get("above_vwap"),
        "levels": snap.get("levels", {"support": [], "resistance": []}),
    }

    # Prefer the shared quote service's fresher intraday price when it has one.
    try:
        from dashboard import quotes

        quote = quotes.get_quote(symbol, include_prev_close=True)
        if quote.get("price") is not None:
            payload["price"] = round(float(quote["price"]), 4)
            payload["change_pct"] = quote.get("change_pct")
    except Exception:  # noqa: BLE001 -- daily close is a fine fallback
        # The daily close is a fine fallback, but log the live-quote failure so
        # a persistently broken quote provider is visible (B-9).
        log.debug("ta.live_quote_failed", symbol=symbol, exc_info=True)

    _cache_put(_indicator_cache, symbol, payload, INDICATOR_CACHE_TTL)
    return payload


def _live_block(
    symbol: str, pos: Dict[str, Any], settings
) -> Optional[Dict[str, Any]]:
    """Live readings + risk math for the explanation's "Now:" sentence."""
    try:
        live = dict(_live_indicator_payload(symbol, settings))
        entry = float(pos.get("entry_price", 0) or 0)
        stop = float(pos.get("stop_price", 0) or 0)
        target = float(pos.get("target_price", 0) or 0)
        price = live.get("price")
        if price and entry > 0:
            side = str(pos.get("direction", "long") or "long").lower()
            sign = -1.0 if side == "short" else 1.0
            move = (float(price) - entry) * sign
            risk = (entry - stop) * sign
            if risk > 0:
                live["r_progress"] = round(move / risk, 2)
            if stop:
                live["distance_to_stop_pct"] = round(
                    abs(float(price) - stop) / float(price) * 100.0, 2
                )
            if target:
                live["distance_to_target_pct"] = round(
                    abs(target - float(price)) / float(price) * 100.0, 2
                )
        return live
    except HTTPException:
        return None
    except Exception:  # noqa: BLE001 -- the chart works without live data
        log.debug("ta.live_block_failed", symbol=symbol, exc_info=True)
        return None


@router.get("/{symbol}/ta-chart")
async def ta_chart(
    symbol: str,
    entry_time: str = "",
    _user: str = Depends(require_auth),
):
    """Full TA chart payload for one trade (snapshot-first, recompute fallback)."""
    settings = get_settings()
    data_dir = Path(settings.DATA_DIR)
    sym = symbol.upper().strip()

    rec = find_rationale(data_dir, sym, entry_time or None)
    pos = _load_open_position(data_dir, sym)
    hist = None
    if rec is None and pos is None:
        hist = _completed_trade(data_dir, sym, entry_time)
    trade = _trade_info(rec, pos, hist)
    if trade is None:
        raise HTTPException(
            status_code=404,
            detail=f"No trade record found for {sym}.",
        )

    indicators = (rec or {}).get("indicators") or {}
    bars = (rec or {}).get("bars") or []
    if indicators.get("series") and bars:
        source = "snapshot"
        series = indicators.get("series", {})
        atr14 = indicators.get("atr14")
        levels = indicators.get("levels", {"support": [], "resistance": []})
        state = indicators.get("state", {})
    else:
        source = "recomputed"
        cache_key = f"{sym}|{str(trade.get('entry_time') or '')[:19]}"
        snap = _cache_get(_chart_cache, cache_key)
        if snap is None:
            snap = _recompute_snapshot(sym, settings)
            if snap is not None:
                _cache_put(_chart_cache, cache_key, snap, CHART_CACHE_TTL)
        if snap is None:
            raise HTTPException(
                status_code=404,
                detail=f"No OHLCV data available to reconstruct {sym}.",
            )
        bars = snap.get("bars", [])
        series = snap.get("series", {})
        atr14 = snap.get("atr14")
        levels = snap.get("levels", {"support": [], "resistance": []})
        state = snap.get("state", {})

    live = _live_block(sym, pos, settings) if pos else None
    explanation = build_explanation(
        trade,
        state,
        atr14,
        levels,
        atr_multiplier=float(settings.ATR_STOP_MULTIPLIER),
        rr_min=float(settings.RISK_REWARD_MIN),
        live=live,
        criteria=(rec or {}).get("criteria"),
    )

    return {
        "symbol": sym,
        "source": source,
        "trade": trade,
        "bars": bars,
        "series": series,
        "levels": levels,
        "annotations": {
            "pattern": _STRATEGY_PATTERN.get(
                str(trade.get("strategy") or "").lower(),
                f"{trade.get('strategy') or 'unknown'} setup",
            ),
            "entry_bar": _entry_bar_index(bars, trade.get("entry_time")),
            "atr14": atr14,
        },
        "state": state,
        "live": live,
        "explanation": explanation,
    }


@router.get("/{symbol}/indicators")
async def live_indicators(
    symbol: str,
    _user: str = Depends(require_auth),
):
    """Current live indicator values for one symbol (open-position view)."""
    settings = get_settings()
    return _live_indicator_payload(symbol.upper().strip(), settings)
