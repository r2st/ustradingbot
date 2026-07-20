"""
Lightweight, dependency-free application metrics for the dashboard (audit B-3).

A tiny hand-rolled registry — no ``prometheus_client`` dependency — that is
nonetheless exposed in the Prometheus text exposition format at ``/metrics`` so
any standard scraper can consume it.  Three primitive types are supported:

* **counters** — monotonically increasing totals (requests, orders, errors, LLM
  calls), optionally labelled;
* **gauges** — point-in-time values that can go up or down (set at scrape time
  from the engine heartbeat: open positions, engine liveness);
* **histograms** — a running ``sum`` + ``count`` (+ fixed buckets) for latency
  distributions (request duration, provider latency).

All state is process-local and guarded by a single lock — the same in-process
model as :mod:`dashboard.rate_limit`.  Under multiple workers each process
exports its own series (Prometheus handles that natively via the ``instance``
label); the readiness/liveness truth still comes from the shared heartbeat file,
so multi-worker deployments stay correct.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

_lock = threading.Lock()

# name -> {label_key -> value}.  label_key is a stable tuple of (k, v) pairs.
_LabelKey = Tuple[Tuple[str, str], ...]
_counters: Dict[str, Dict[_LabelKey, float]] = {}
_gauges: Dict[str, Dict[_LabelKey, float]] = {}
# name -> {label_key -> (sum, count, {bucket_le: count})}
_histograms: Dict[str, Dict[_LabelKey, "list"]] = {}

# Metadata: name -> (type, help text)
_meta: Dict[str, Tuple[str, str]] = {}

#: Default latency buckets (milliseconds) for request/provider histograms.
_DEFAULT_BUCKETS_MS: Tuple[float, ...] = (
    5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000
)

# Gauge callbacks evaluated lazily at scrape time (e.g. engine heartbeat).
_scrape_hooks: List[Callable[[], None]] = []


def _key(labels: Optional[Dict[str, str]]) -> _LabelKey:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _register(name: str, mtype: str, help_text: str) -> None:
    _meta.setdefault(name, (mtype, help_text))


def reset() -> None:
    """Clear every series (used by the test-suite between tests)."""
    with _lock:
        _counters.clear()
        _gauges.clear()
        _histograms.clear()


def inc(
    name: str,
    amount: float = 1.0,
    labels: Optional[Dict[str, str]] = None,
    *,
    help_text: str = "",
) -> None:
    """Increment counter *name* by *amount* (default 1)."""
    _register(name, "counter", help_text or name)
    k = _key(labels)
    with _lock:
        series = _counters.setdefault(name, {})
        series[k] = series.get(k, 0.0) + amount


def set_gauge(
    name: str,
    value: float,
    labels: Optional[Dict[str, str]] = None,
    *,
    help_text: str = "",
) -> None:
    """Set gauge *name* to *value*."""
    _register(name, "gauge", help_text or name)
    k = _key(labels)
    with _lock:
        _gauges.setdefault(name, {})[k] = float(value)


def observe(
    name: str,
    value_ms: float,
    labels: Optional[Dict[str, str]] = None,
    *,
    buckets: Tuple[float, ...] = _DEFAULT_BUCKETS_MS,
    help_text: str = "",
) -> None:
    """Record *value_ms* into histogram *name* (sum + count + buckets)."""
    _register(name, "histogram", help_text or name)
    k = _key(labels)
    with _lock:
        series = _histograms.setdefault(name, {})
        rec = series.get(k)
        if rec is None:
            rec = [0.0, 0, {b: 0 for b in buckets}]
            series[k] = rec
        rec[0] += value_ms
        rec[1] += 1
        for b in rec[2]:
            if value_ms <= b:
                rec[2][b] += 1


def register_scrape_hook(hook: Callable[[], None]) -> None:
    """Register a callback run at scrape time to refresh gauges lazily."""
    _scrape_hooks.append(hook)


def time_block(name: str, labels: Optional[Dict[str, str]] = None):
    """Context manager timing a block into histogram *name* (milliseconds)."""

    class _Timer:
        def __enter__(self):
            self._start = time.perf_counter()
            return self

        def __exit__(self, *exc):
            observe(name, (time.perf_counter() - self._start) * 1000.0, labels)
            return False

    return _Timer()


def _fmt_labels(k: _LabelKey, extra: Optional[Tuple[str, str]] = None) -> str:
    pairs = list(k)
    if extra is not None:
        pairs = pairs + [extra]
    if not pairs:
        return ""
    inner = ",".join(f'{name}="{_escape(val)}"' for name, val in pairs)
    return "{" + inner + "}"


def _escape(val: str) -> str:
    return val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render() -> str:
    """Render every series in the Prometheus text exposition format."""
    for hook in list(_scrape_hooks):
        try:
            hook()
        except Exception:  # noqa: BLE001 — a broken gauge hook must not 500 /metrics
            continue

    lines: List[str] = []
    with _lock:
        for name in sorted(_meta):
            mtype, help_text = _meta[name]
            if name in _counters:
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} counter")
                for k, v in sorted(_counters[name].items()):
                    lines.append(f"{name}{_fmt_labels(k)} {_num(v)}")
            elif name in _gauges:
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} gauge")
                for k, v in sorted(_gauges[name].items()):
                    lines.append(f"{name}{_fmt_labels(k)} {_num(v)}")
            elif name in _histograms:
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} histogram")
                for k, (total, count, buckets) in sorted(
                    _histograms[name].items(), key=lambda kv: kv[0]
                ):
                    # Bucket counts are already cumulative: each observe()
                    # increments every bucket whose le >= the value.
                    for b in sorted(buckets):
                        lines.append(
                            f"{name}_bucket"
                            f"{_fmt_labels(k, ('le', _num(b)))} {buckets[b]}"
                        )
                    lines.append(
                        f"{name}_bucket{_fmt_labels(k, ('le', '+Inf'))} {count}"
                    )
                    lines.append(f"{name}_sum{_fmt_labels(k)} {_num(total)}")
                    lines.append(f"{name}_count{_fmt_labels(k)} {count}")
    return "\n".join(lines) + "\n"


def _num(v: float) -> str:
    """Render a number without a trailing ``.0`` for integers."""
    if v == int(v):
        return str(int(v))
    return repr(v)
