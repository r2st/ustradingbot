"""B14 — mtime-guarded cache for the trade-journal CSV read."""

from __future__ import annotations

import pandas as pd

from analytics import performance


_HEADER = "symbol,strategy,exit_time,pnl_net,r_multiple\n"


def _write(path, rows):
    path.write_text(_HEADER + "".join(rows))


def test_cache_hit_avoids_reparse(tmp_path, monkeypatch):
    csv = tmp_path / "trades.csv"
    _write(csv, ["AAPL,momentum,2026-01-02T10:00:00,100,1.5\n"])
    performance.clear_trades_cache()

    calls = {"n": 0}
    real = performance._load_completed_trades_uncached

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(performance, "_load_completed_trades_uncached", counting)

    a = performance.load_completed_trades(csv)
    b = performance.load_completed_trades(csv)
    assert calls["n"] == 1  # second read served from cache
    assert len(a) == len(b) == 1


def test_cache_invalidates_on_write(tmp_path):
    csv = tmp_path / "trades.csv"
    _write(csv, ["AAPL,momentum,2026-01-02T10:00:00,100,1.5\n"])
    performance.clear_trades_cache()

    first = performance.load_completed_trades(csv)
    assert len(first) == 1

    # Append a second completed trade — mtime + size change must bust the cache.
    _write(
        csv,
        [
            "AAPL,momentum,2026-01-02T10:00:00,100,1.5\n",
            "MSFT,swing,2026-01-03T10:00:00,50,0.8\n",
        ],
    )
    second = performance.load_completed_trades(csv)
    assert len(second) == 2


def test_cache_returns_independent_copies(tmp_path):
    csv = tmp_path / "trades.csv"
    _write(csv, ["AAPL,momentum,2026-01-02T10:00:00,100,1.5\n"])
    performance.clear_trades_cache()

    a = performance.load_completed_trades(csv)
    a.loc[0, "pnl_net"] = -999  # mutate the returned frame
    b = performance.load_completed_trades(csv)
    assert float(b.loc[0, "pnl_net"]) == 100.0  # cache not corrupted


def test_missing_file_returns_empty(tmp_path):
    performance.clear_trades_cache()
    out = performance.load_completed_trades(tmp_path / "nope.csv")
    assert isinstance(out, pd.DataFrame)
    assert out.empty
