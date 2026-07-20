"""
Custom indicator & portfolio alerts (P1-4).

Extends the simple price-cross alerts (:mod:`alerts.price_alerts`) with the
condition types traders actually watch:

* ``rsi_cross`` — RSI(14) crosses above/below a threshold.
* ``ma_cross`` — a fast MA crosses a slow MA (golden / death cross).
* ``volume_spike`` — volume runs X% above its 20-day average.
* ``drawdown`` — portfolio (or a single position) draws down past a threshold.
* ``daily_loss`` — today's realised loss reaches a fraction of capital.

The *evaluators* are pure functions over pandas Series / scalars so they unit
test with no I/O.  :class:`IndicatorAlertStore` persists rules to
``DATA_DIR/indicator_alerts.json`` (atomic writes, same conventions as the
price-alert store), and :func:`check_indicator_alerts` wires the two together:
it fetches the data each armed rule needs, fires a push notification on a
trigger, and marks the rule so it does not re-fire until re-armed.  It never
raises into the engine's scan cycle.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
import structlog

from config.settings import EASTERN

log = structlog.get_logger(__name__)

_FILENAME = "indicator_alerts.json"
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,6}(\.[A-Z]{1,3})?$")
_MAX_NOTE = 200

RULE_TYPES = ("rsi_cross", "ma_cross", "volume_spike", "drawdown", "daily_loss")
#: Rule types that watch the whole book rather than one symbol.
PORTFOLIO_TYPES = ("drawdown", "daily_loss")


class IndicatorAlertError(ValueError):
    """Raised when an indicator-alert operation gets invalid input."""


def _now_iso() -> str:
    return datetime.now(tz=EASTERN).isoformat(timespec="seconds")


def _normalize_symbol(symbol: str) -> str:
    sym = str(symbol or "").strip().upper()
    if not sym or not _SYMBOL_RE.match(sym):
        raise IndicatorAlertError(f"Invalid symbol: {symbol!r}")
    return sym


# ---------------------------------------------------------------------------
# Pure evaluators
# ---------------------------------------------------------------------------


def evaluate_rsi_cross(
    rsi: pd.Series, direction: str, threshold: float
) -> Optional[float]:
    """Return the RSI value when it *crosses* the threshold this bar, else None.

    A cross requires the previous bar on one side and the latest on the other,
    so a series that merely stays above the threshold does not re-fire.
    """
    s = rsi.dropna()
    if len(s) < 2:
        return None
    prev, last = float(s.iloc[-2]), float(s.iloc[-1])
    if direction == "above" and prev < threshold <= last:
        return round(last, 2)
    if direction == "below" and prev > threshold >= last:
        return round(last, 2)
    return None


def evaluate_ma_cross(
    close: pd.Series, fast: int, slow: int, kind: str
) -> Optional[Dict[str, float]]:
    """Return the MA values on a golden/death cross this bar, else None.

    ``kind='golden'`` fires when the fast MA crosses *above* the slow MA;
    ``kind='death'`` when it crosses *below*.  Uses simple moving averages.
    """
    if fast >= slow:
        raise IndicatorAlertError("fast period must be less than slow period")
    c = close.astype(float)
    if len(c) < slow + 1:
        return None
    fast_ma = c.rolling(fast).mean()
    slow_ma = c.rolling(slow).mean()
    if pd.isna(fast_ma.iloc[-2]) or pd.isna(slow_ma.iloc[-2]):
        return None
    pf, ps = float(fast_ma.iloc[-2]), float(slow_ma.iloc[-2])
    lf, ls = float(fast_ma.iloc[-1]), float(slow_ma.iloc[-1])
    crossed_up = pf <= ps and lf > ls
    crossed_down = pf >= ps and lf < ls
    if (kind == "golden" and crossed_up) or (kind == "death" and crossed_down):
        return {"fast_ma": round(lf, 4), "slow_ma": round(ls, 4)}
    return None


def evaluate_volume_spike(
    volume: pd.Series, pct_above_avg: float, window: int = 20
) -> Optional[Dict[str, float]]:
    """Return details when the latest volume exceeds its average by *pct_above_avg*.

    ``pct_above_avg`` is a percentage (e.g. 50 → 50% above the 20-day average).
    """
    v = volume.astype(float).dropna()
    if len(v) < window + 1:
        return None
    avg = float(v.iloc[-(window + 1):-1].mean())
    last = float(v.iloc[-1])
    if avg <= 0:
        return None
    ratio = last / avg
    if ratio >= 1.0 + pct_above_avg / 100.0:
        return {"volume": round(last, 0), "avg": round(avg, 0),
                "pct_above": round((ratio - 1.0) * 100.0, 1)}
    return None


def evaluate_threshold(value: Optional[float], threshold: float) -> Optional[float]:
    """Return *value* when it meets/exceeds *threshold* (for drawdown/daily-loss).

    Both are expressed as positive magnitudes (a 12% drawdown is ``12.0``).
    """
    if value is None:
        return None
    if float(value) >= float(threshold):
        return round(float(value), 2)
    return None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class IndicatorAlertStore:
    """Thread-safe JSON-backed store of indicator/portfolio alert rules."""

    def __init__(self, data_dir: str | Path) -> None:
        self._dir = Path(data_dir)
        self._path = self._dir / _FILENAME
        self._lock = RLock()

    def _load(self) -> List[Dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("indicator_alerts.load_failed", error=str(exc))
            return []
        items = data.get("alerts") if isinstance(data, dict) else data
        return list(items) if isinstance(items, list) else []

    def _save(self, alerts: List[Dict[str, Any]]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"alerts": alerts}, f, indent=2)
            os.replace(tmp, self._path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def list_alerts(self) -> List[Dict[str, Any]]:
        with self._lock:
            return self._load()

    def add_alert(self, rule_type: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Validate + persist a new rule; returns the stored rule."""
        rule = _validate_rule(rule_type, params)
        with self._lock:
            alerts = self._load()
            alerts.append(rule)
            self._save(alerts)
        return rule

    def delete_alert(self, alert_id: str) -> bool:
        with self._lock:
            alerts = self._load()
            kept = [a for a in alerts if a.get("id") != alert_id]
            if len(kept) == len(alerts):
                return False
            self._save(kept)
            return True

    def set_active(self, alert_id: str, active: bool) -> Optional[Dict[str, Any]]:
        with self._lock:
            alerts = self._load()
            found = None
            for a in alerts:
                if a.get("id") == alert_id:
                    a["active"] = bool(active)
                    if active:
                        a["triggered_at"] = None
                    found = a
                    break
            if found is None:
                return None
            self._save(alerts)
            return found

    def _persist_all(self, alerts: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._save(alerts)


def _validate_rule(rule_type: str, params: Dict[str, Any]) -> Dict[str, Any]:
    rt = str(rule_type or "").strip().lower()
    if rt not in RULE_TYPES:
        raise IndicatorAlertError(f"type must be one of {list(RULE_TYPES)}")
    rule: Dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "type": rt,
        "active": True,
        "created_at": _now_iso(),
        "triggered_at": None,
        "last_value": None,
        "note": str(params.get("note", "") or "")[:_MAX_NOTE],
    }

    def _f(key: str) -> float:
        try:
            return float(params[key])
        except (KeyError, TypeError, ValueError):
            raise IndicatorAlertError(f"{key} must be a number.")

    if rt == "rsi_cross":
        rule["symbol"] = _normalize_symbol(params.get("symbol", ""))
        direction = str(params.get("direction", "")).strip().lower()
        if direction not in ("above", "below"):
            raise IndicatorAlertError("direction must be 'above' or 'below'")
        rule["direction"] = direction
        rule["threshold"] = round(_f("threshold"), 2)
    elif rt == "ma_cross":
        rule["symbol"] = _normalize_symbol(params.get("symbol", ""))
        kind = str(params.get("kind", "golden")).strip().lower()
        if kind not in ("golden", "death"):
            raise IndicatorAlertError("kind must be 'golden' or 'death'")
        rule["kind"] = kind
        rule["fast"] = int(params.get("fast", 50))
        rule["slow"] = int(params.get("slow", 200))
        if rule["fast"] >= rule["slow"]:
            raise IndicatorAlertError("fast period must be less than slow period")
    elif rt == "volume_spike":
        rule["symbol"] = _normalize_symbol(params.get("symbol", ""))
        rule["pct_above_avg"] = round(_f("pct_above_avg"), 2)
        if rule["pct_above_avg"] <= 0:
            raise IndicatorAlertError("pct_above_avg must be positive")
    elif rt == "drawdown":
        scope = str(params.get("scope", "portfolio")).strip().lower()
        if scope not in ("portfolio", "position"):
            raise IndicatorAlertError("scope must be 'portfolio' or 'position'")
        rule["scope"] = scope
        if scope == "position":
            rule["symbol"] = _normalize_symbol(params.get("symbol", ""))
        rule["threshold_pct"] = round(_f("threshold_pct"), 2)
    elif rt == "daily_loss":
        rule["threshold_pct"] = round(_f("threshold_pct"), 2)
    return rule


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------


_stores: Dict[str, IndicatorAlertStore] = {}
_stores_lock = RLock()


def get_indicator_alert_store(data_dir: str | Path) -> IndicatorAlertStore:
    key = str(Path(data_dir))
    with _stores_lock:
        store = _stores.get(key)
        if store is None:
            store = IndicatorAlertStore(data_dir)
            _stores[key] = store
        return store


def check_indicator_alerts(
    settings: Any,
    ohlcv_fetcher: Optional[Callable[[str], Any]] = None,
    portfolio_ctx: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Evaluate every armed rule; fire + mark those that trigger.

    Args:
        settings: app settings (needs ``DATA_DIR``).
        ohlcv_fetcher: ``symbol -> OHLCV DataFrame``.  Defaults to
            :func:`data.fetcher.fetch_ohlcv`.  Injectable for tests.
        portfolio_ctx: ``{"drawdown_pct": float, "daily_loss_pct": float,
            "position_drawdowns": {symbol: pct}}`` — the portfolio state the
            portfolio-scoped rules test against.  Missing keys disable those
            rules (they simply do not fire).

    Returns the list of rules that triggered.  Never raises.
    """
    store = get_indicator_alert_store(settings.DATA_DIR)
    try:
        alerts = store.list_alerts()
    except Exception as exc:  # noqa: BLE001
        log.warning("indicator_alerts.check_load_failed", error=str(exc))
        return []

    armed = [a for a in alerts if a.get("active") and not a.get("triggered_at")]
    if not armed:
        return []

    ctx = portfolio_ctx or {}
    fetch = ohlcv_fetcher
    if fetch is None:
        try:
            from data.fetcher import fetch_ohlcv as fetch  # type: ignore
        except Exception:  # noqa: BLE001
            fetch = None

    # Cache OHLCV fetches within this pass (several rules may share a symbol).
    df_cache: Dict[str, Any] = {}

    def _df(symbol: str) -> Any:
        if symbol not in df_cache:
            try:
                df_cache[symbol] = fetch(symbol) if fetch else None
            except Exception:  # noqa: BLE001
                df_cache[symbol] = None
        return df_cache[symbol]

    triggered: List[Dict[str, Any]] = []
    changed = False
    for rule in alerts:
        if not rule.get("active") or rule.get("triggered_at"):
            continue
        try:
            hit = _evaluate_rule(rule, _df, ctx)
        except Exception as exc:  # noqa: BLE001 -- one bad rule can't stop the rest
            log.warning("indicator_alerts.rule_failed", rule_id=rule.get("id"),
                        error=str(exc))
            continue
        if hit is not None:
            rule["last_value"] = hit
            rule["triggered_at"] = _now_iso()
            changed = True
            triggered.append(dict(rule))
            _publish(settings, rule, hit)

    if changed:
        try:
            store._persist_all(alerts)
        except Exception as exc:  # noqa: BLE001
            log.warning("indicator_alerts.persist_failed", error=str(exc))

    if triggered:
        log.info("indicator_alerts.triggered", count=len(triggered),
                 types=[t["type"] for t in triggered])
    return triggered


def _evaluate_rule(
    rule: Dict[str, Any], df_of: Callable[[str], Any], ctx: Dict[str, Any]
) -> Optional[Any]:
    """Return a truthy trigger value when *rule* fires, else None."""
    rt = rule.get("type")
    if rt == "daily_loss":
        return evaluate_threshold(ctx.get("daily_loss_pct"), rule["threshold_pct"])
    if rt == "drawdown":
        if rule.get("scope") == "position":
            dd = (ctx.get("position_drawdowns") or {}).get(rule.get("symbol"))
        else:
            dd = ctx.get("drawdown_pct")
        return evaluate_threshold(dd, rule["threshold_pct"])

    # Symbol/indicator rules need OHLCV.
    df = df_of(str(rule.get("symbol", "")))
    if df is None or getattr(df, "empty", True):
        return None
    if rt == "rsi_cross":
        from signals.indicator_snapshot import compute_indicator_series

        rsi = compute_indicator_series(df).get("rsi")
        if rsi is None:
            return None
        return evaluate_rsi_cross(rsi, rule["direction"], rule["threshold"])
    if rt == "ma_cross":
        return evaluate_ma_cross(df["Close"], rule["fast"], rule["slow"], rule["kind"])
    if rt == "volume_spike":
        return evaluate_volume_spike(df["Volume"], rule["pct_above_avg"])
    return None


def _describe(rule: Dict[str, Any], value: Any) -> str:
    rt = rule.get("type")
    sym = rule.get("symbol", "")
    if rt == "rsi_cross":
        return f"{sym} RSI crossed {rule['direction']} {rule['threshold']} (now {value})"
    if rt == "ma_cross":
        return f"{sym} {rule['kind']} cross ({rule['fast']}/{rule['slow']} MA)"
    if rt == "volume_spike":
        pct = value.get("pct_above") if isinstance(value, dict) else value
        return f"{sym} volume spike: {pct}% above average"
    if rt == "drawdown":
        scope = sym if rule.get("scope") == "position" else "Portfolio"
        return f"{scope} drawdown {value}% ≥ {rule['threshold_pct']}%"
    if rt == "daily_loss":
        return f"Daily loss {value}% ≥ {rule['threshold_pct']}% limit"
    return f"{sym} alert triggered"


def _publish(settings: Any, rule: Dict[str, Any], value: Any) -> None:
    """Best-effort push notification for a triggered rule."""
    try:
        from dashboard.push import publish

        title = f"{rule.get('symbol') or 'Portfolio'} alert"
        body = _describe(rule, value)
        if rule.get("note"):
            body += f" — {rule['note']}"
        publish(title, body, settings.DATA_DIR, category="ai_alert")
    except Exception as exc:  # noqa: BLE001
        log.warning("indicator_alerts.publish_failed", error=str(exc))
