"""Tests for automated P&L reports and scheduled backtests (features 10 & 11)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from automation.pnl_report import build_report, send_report
from automation.scheduled_backtest import format_summary, run_backtests
from config.settings import Settings
from journal.trade_logger import SCHEMA_COLUMNS


def _write_journal(data_dir: Path, rows):
    header = ",".join(SCHEMA_COLUMNS)
    lines = [header]
    for r in rows:
        row = {c: "" for c in SCHEMA_COLUMNS}
        row.update(r)
        lines.append(",".join(str(row[c]) for c in SCHEMA_COLUMNS))
    (data_dir / "trades.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_build_report_no_journal(tmp_data_dir):
    settings = Settings(DATA_DIR=tmp_data_dir, TOTAL_CAPITAL=10_000)
    content = build_report(settings, "daily", now=datetime(2026, 7, 1, 17, 0))
    assert "Daily" in content.subject
    assert content.body  # non-empty even with no trades


def test_build_report_with_trades(tmp_data_dir):
    _write_journal(tmp_data_dir, [
        {"trade_id": 1, "symbol": "AAPL", "strategy": "momentum",
         "entry_fill_price": 100, "quantity": 10, "exit_price": 110,
         "exit_time": "2026-07-01T15:00:00", "pnl_net": 100},
        {"trade_id": 2, "symbol": "MSFT", "strategy": "swing",
         "entry_fill_price": 100, "quantity": 10, "exit_price": 95,
         "exit_time": "2026-07-01T15:30:00", "pnl_net": -50},
    ])
    settings = Settings(DATA_DIR=tmp_data_dir, TOTAL_CAPITAL=10_000)
    content = build_report(settings, "daily", now=datetime(2026, 7, 1, 17, 0))
    assert "Total trades" in content.body
    assert "momentum" in content.body


def test_send_report_disabled_email(tmp_data_dir):
    settings = Settings(DATA_DIR=tmp_data_dir, TOTAL_CAPITAL=10_000, EMAIL_ALERTS_ENABLED=False)

    class FakeNotifier:
        enabled = False

    assert send_report(settings, "daily", notifier=FakeNotifier()) is False


def test_send_report_sends_when_enabled(tmp_data_dir):
    settings = Settings(DATA_DIR=tmp_data_dir, TOTAL_CAPITAL=10_000)
    sent = {}

    class FakeNotifier:
        enabled = True

        def send_sync(self, subject, body):
            sent["subject"] = subject
            return True

    assert send_report(settings, "weekly", notifier=FakeNotifier(),
                       now=datetime(2026, 7, 3, 17, 0)) is True
    assert "Weekly" in sent["subject"]


class _FakeResult:
    def __init__(self, strategy):
        self.summary = {"total_trades": 3, "win_rate": 0.66,
                        "profit_factor": 1.8, "total_pnl": 120.0}
        self.saved = None

    def save(self, path):
        self.saved = path


def test_run_backtests_uses_injected_runner(tmp_data_dir):
    settings = Settings(DATA_DIR=tmp_data_dir, TOTAL_CAPITAL=10_000,
                        SCHEDULED_BACKTEST_LOOKBACK_DAYS=90)
    seen = []

    def runner(cfg):
        seen.append(cfg.strategies[0])
        return _FakeResult(cfg.strategies[0])

    report = run_backtests(settings, today=date(2026, 7, 1), runner=runner, write=False)
    assert report.symbols > 0
    assert len(report.per_strategy) == len(seen) >= 4
    assert all(r["total_trades"] == 3 for r in report.per_strategy)
    assert format_summary(report)


def test_run_backtests_isolates_strategy_error(tmp_data_dir):
    settings = Settings(DATA_DIR=tmp_data_dir, TOTAL_CAPITAL=10_000)

    def runner(cfg):
        if cfg.strategies[0] == "momentum":
            raise RuntimeError("boom")
        return _FakeResult(cfg.strategies[0])

    report = run_backtests(settings, today=date(2026, 7, 1), runner=runner, write=False)
    errored = [r for r in report.per_strategy if "error" in r]
    assert any(r["strategy"] == "momentum" for r in errored)
    # Other strategies still produced results.
    assert any("total_trades" in r for r in report.per_strategy)
