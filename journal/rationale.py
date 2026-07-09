"""
Trade rationale capture (monitoring feature 9).

When the engine places a trade it records *why*: a set of scored criteria
(0–10 each, with a one-line explanation) derived from the signal's indicator
scores and the entry-pipeline context (AI veto verdict, market regime,
risk/reward), plus a snapshot of the daily bars around the entry so the
dashboard can draw the breakout pattern with entry/stop/target levels — even
months later, exactly as the setup looked at entry time.

Records are appended (one JSON line each) to ``DATA_DIR/trade_rationale.jsonl``
by :class:`RationaleStore`.  All writes are best-effort: a rationale failure
must never block an entry.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from config.settings import EASTERN

log = structlog.get_logger(__name__)

RATIONALE_FILE = "trade_rationale.jsonl"

#: Rotate the rationale JSONL when it exceeds this size (~50 MB — v2 records
#: carry a bar snapshot *and* the full indicator series, so they are much
#: bigger than activity events; ~25-40 KB per trade).
MAX_RATIONALE_BYTES = 50 * 1024 * 1024

#: How many daily bars to snapshot around the entry for the pattern chart.
CHART_BARS = 90

_STRATEGY_PATTERN = {
    "vcp_breakout": "VCP volatility-contraction breakout",
    "momentum": "momentum trend-continuation breakout",
    "swing": "oversold pullback in an uptrend",
    "mean_reversion": "extreme-oversold mean-reversion setup",
    "pead": "post-earnings announcement drift",
}


def _clamp10(x: float) -> float:
    return round(min(10.0, max(0.0, float(x))), 1)


def build_trade_rationale(
    signal: Any,
    *,
    ai_decision: Optional[Any] = None,
    regime: Optional[Any] = None,
    regime_multiplier: Optional[float] = None,
    risk_reward_min: float = 1.8,
) -> List[Dict[str, Any]]:
    """Build the scored criteria list for a trade (pure; no I/O).

    Args:
        signal: The :class:`~signals.signal_types.Signal` being traded.
        ai_decision: The :class:`~ai.analyst.AIDecision` for this signal.
        regime: The cycle's ``RegimeResult`` (has ``.regime``), if available.
        regime_multiplier: The regime weight multiplier for this strategy
            family, if computed.
        risk_reward_min: The configured minimum R:R (for the explanation).

    Returns:
        A list of ``{key, name, score, explanation}`` dicts, scores in [0, 10].
    """
    strategy = str(getattr(signal, "strategy", "") or "").lower()
    criteria: List[Dict[str, Any]] = []

    # 1. Setup grade quality — the combined weighted score itself.
    strength = float(getattr(signal, "signal_strength", 0.0) or 0.0)
    grade = getattr(getattr(signal, "grade", None), "value", "?")
    criteria.append({
        "key": "setup_grade",
        "name": "Setup grade quality",
        "score": _clamp10(strength * 10.0),
        "explanation": f"Combined weighted signal score {strength:.2f} → grade {grade}",
    })

    # 2. Breakout pattern strength — structure blend, named by strategy.
    ema = float(getattr(signal, "ema_score", 0.0) or 0.0)
    ripster = float(getattr(signal, "ripster_score", 0.0) or 0.0)
    pattern = _STRATEGY_PATTERN.get(strategy, f"{strategy or 'unknown'} setup")
    criteria.append({
        "key": "breakout_pattern",
        "name": "Breakout pattern strength",
        "score": _clamp10((ema + ripster) / 2.0 * 10.0),
        "explanation": (
            f"{pattern.capitalize()} — EMA structure {ema:.2f}, "
            f"Ripster cloud {ripster:.2f}"
        ),
    })

    # 3. Volume confirmation.
    vol_score = float(getattr(signal, "volume_score", 0.0) or 0.0)
    vol_ratio = float(getattr(signal, "volume_ratio", 0.0) or 0.0)
    criteria.append({
        "key": "volume_confirmation",
        "name": "Volume confirmation",
        "score": _clamp10(vol_score * 10.0),
        "explanation": f"Volume {vol_ratio:.1f}× its 20-day average",
    })

    # 4. Trend alignment.
    criteria.append({
        "key": "trend_alignment",
        "name": "Trend alignment",
        "score": _clamp10(ema * 10.0),
        "explanation": f"EMA stack alignment score {ema:.2f} (price vs EMA 8/21/50/200)",
    })

    # 5. Momentum (MACD).
    macd_score = float(getattr(signal, "macd_score", 0.0) or 0.0)
    macd_hist = float(getattr(signal, "macd_histogram", 0.0) or 0.0)
    criteria.append({
        "key": "momentum",
        "name": "MACD momentum",
        "score": _clamp10(macd_score * 10.0),
        "explanation": f"MACD histogram {macd_hist:+.3f} ({'rising' if macd_hist > 0 else 'flat/negative'})",
    })

    # 6. RSI positioning.
    rsi_score = float(getattr(signal, "rsi_score", 0.0) or 0.0)
    rsi_value = float(getattr(signal, "rsi_value", 0.0) or 0.0)
    criteria.append({
        "key": "rsi_positioning",
        "name": "RSI positioning",
        "score": _clamp10(rsi_score * 10.0),
        "explanation": f"RSI(14) at {rsi_value:.0f}",
    })

    # 7. Risk/reward ratio — 10 at 3:1 or better.
    try:
        rr = float(getattr(signal, "risk_reward_ratio", 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        rr = 0.0
    criteria.append({
        "key": "risk_reward",
        "name": "Risk/reward ratio",
        "score": _clamp10(rr / 3.0 * 10.0),
        "explanation": f"R:R {rr:.2f}:1 vs {risk_reward_min:g}:1 required",
    })

    # 8. AI veto score — the trade was approved, or it would not exist; the
    #    score reflects how it was approved and the reasoning is preserved.
    if ai_decision is not None:
        tier = str(getattr(ai_decision, "tier", "") or "")
        reasoning = str(getattr(ai_decision, "reasoning", "") or "")[:240]
        ai_score = 9.0 if tier in ("tier2", "cache") else 7.0
        criteria.append({
            "key": "ai_veto",
            "name": "AI veto score",
            "score": _clamp10(ai_score),
            "explanation": f"{getattr(ai_decision, 'decision', 'APPROVE')} — {reasoning}",
        })

    # 9. Volatility-regime fit.
    if regime is not None or regime_multiplier is not None:
        mult = float(regime_multiplier if regime_multiplier is not None else 1.0)
        regime_name = str(getattr(regime, "regime", "neutral") or "neutral")
        criteria.append({
            "key": "regime_fit",
            "name": "Volatility regime fit",
            "score": _clamp10(mult / 1.25 * 10.0),
            "explanation": f"{regime_name} regime — {strategy or 'strategy'} weight ×{mult:.2f}",
        })

    # 10. Relative strength / OBV confirmation.
    obv = bool(getattr(signal, "obv_confirming", False))
    criteria.append({
        "key": "obv_relative_strength",
        "name": "OBV / relative strength",
        "score": 9.0 if obv else 4.0,
        "explanation": (
            "On-balance volume confirms the price trend"
            if obv
            else "On-balance volume does not confirm the move"
        ),
    })

    return criteria


def snapshot_bars(symbol: str, n: int = CHART_BARS) -> List[Dict[str, Any]]:
    """Snapshot the trailing *n* daily bars for the pattern chart.

    Uses the fetch cache (the screener just fetched these bars), so this is
    normally free.  Returns ``[]`` on any failure — the rationale record is
    still useful without the chart.
    """
    try:
        from data.fetcher import fetch_ohlcv

        df = fetch_ohlcv(symbol)
        if df is None or df.empty:
            return []
        tail = df.tail(n)
        bars: List[Dict[str, Any]] = []
        for idx, row in tail.iterrows():
            try:
                bars.append({
                    "t": str(getattr(idx, "date", lambda: idx)())[:10],
                    "o": round(float(row["Open"]), 4),
                    "h": round(float(row["High"]), 4),
                    "l": round(float(row["Low"]), 4),
                    "c": round(float(row["Close"]), 4),
                    "v": int(float(row.get("Volume", 0) or 0)),
                })
            except (ValueError, TypeError, KeyError):
                continue
        return bars
    except Exception:  # noqa: BLE001 -- chart data is best-effort
        log.debug("rationale.bars_failed", symbol=symbol, exc_info=True)
        return []


class RationaleStore:
    """Append-only JSONL store of per-trade rationale records."""

    def __init__(self, data_dir: str | Path) -> None:
        self._path = Path(data_dir) / RATIONALE_FILE
        self._log = log.bind(component="RationaleStore")

    @property
    def path(self) -> Path:
        return self._path

    def record(
        self,
        signal: Any,
        criteria: List[Dict[str, Any]],
        *,
        quantity: int = 0,
        entry_price: Optional[float] = None,
        entry_time: Optional[str] = None,
        bars: Optional[List[Dict[str, Any]]] = None,
        indicators: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist one rationale record. Never raises.

        *indicators* is the optional v2 payload from
        :func:`signals.indicator_snapshot.build_indicator_snapshot`
        (series + ATR + S/R levels + state flags); v1 records without it
        keep working everywhere.
        """
        try:
            strength = float(getattr(signal, "signal_strength", 0.0) or 0.0)
            rec = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "symbol": getattr(signal, "symbol", ""),
                "strategy": getattr(signal, "strategy", ""),
                "direction": getattr(signal, "direction", "long"),
                "grade": getattr(getattr(signal, "grade", None), "value", ""),
                "entry_time": entry_time or datetime.now(tz=EASTERN).isoformat(),
                "entry_price": (
                    float(entry_price)
                    if entry_price is not None
                    else float(getattr(signal, "entry_price", 0.0) or 0.0)
                ),
                "stop_price": float(getattr(signal, "stop_price", 0.0) or 0.0),
                "target_price": float(getattr(signal, "target_price", 0.0) or 0.0),
                "quantity": int(quantity),
                "overall_score": _clamp10(strength * 10.0),
                "criteria": criteria,
                "bars": bars if bars is not None else [],
            }
            if indicators:
                # v2: full indicator series for the TA chart.  The snapshot's
                # own bars are dropped — the top-level ``bars`` key (aligned
                # to the same window) remains the single source of candles.
                rec["indicators"] = {
                    k: v for k, v in indicators.items() if k != "bars"
                }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._maybe_rotate()
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except Exception:  # noqa: BLE001 -- must never block an entry
            self._log.warning("rationale.write_failed", exc_info=True)

    def _maybe_rotate(self) -> None:
        try:
            if self._path.exists() and self._path.stat().st_size >= MAX_RATIONALE_BYTES:
                os.replace(self._path, self._path.with_suffix(self._path.suffix + ".1"))
        except OSError:
            pass


def read_rationales(
    data_dir: str | Path,
    symbol: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return rationale records newest-first (optionally for one symbol)."""
    path = Path(data_dir) / RATIONALE_FILE
    records: List[Dict[str, Any]] = []
    for p in (path.with_suffix(path.suffix + ".1"), path):
        if not p.exists():
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict):
                        records.append(rec)
        except OSError:
            continue
    if symbol:
        sym = symbol.upper()
        records = [r for r in records if str(r.get("symbol", "")).upper() == sym]
    records.reverse()
    return records[: max(1, int(limit))]


def find_rationale(
    data_dir: str | Path,
    symbol: str,
    entry_time: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return the rationale record nearest *entry_time* for *symbol*.

    Without an ``entry_time`` the newest record for the symbol is returned.
    """
    records = read_rationales(data_dir, symbol=symbol, limit=1000)
    if not records:
        return None
    if not entry_time:
        return records[0]

    def _parse(ts: str) -> Optional[datetime]:
        try:
            dt = datetime.fromisoformat(str(ts))
            return dt.replace(tzinfo=None)
        except (ValueError, TypeError):
            return None

    want = _parse(entry_time)
    if want is None:
        return records[0]
    best, best_delta = None, None
    for rec in records:
        got = _parse(rec.get("entry_time", ""))
        if got is None:
            continue
        delta = abs((got - want).total_seconds())
        if best_delta is None or delta < best_delta:
            best, best_delta = rec, delta
    return best or records[0]
