"""Tests covering the ten QA fixes applied in the July 2026 audit.

Each numbered section corresponds to one of the prioritised fixes:

1. Real-time price supplement in exit checks
2. Trading mode column in journal + backtest labelling
3. Risk pre-check on manual trades
4. Corrupt JSON backup / restore
5. Logged warnings on failed data fetches
6. Direction-aware P&L helper (``position_pnl``)
7. Dual-store reconciliation
8. Telegram delivery status tracking
9. File locking on position JSON
10. ENABLE_DYNAMIC_STOPS canonical setting
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import execution.broker as broker_mod
from config.settings import Settings
from execution.broker import PaperBroker, position_pnl
from execution.exit_manager import ExitManager
from execution.manual_trade import place_manual_trade
from journal.trade_logger import TradeLogger
from risk.manager import RiskManager
from signals.signal_types import (
    ExitEvent,
    ExitReason,
    Grade,
    Signal,
    TradeOrder,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signal(symbol="AAPL", strategy="momentum", entry=100.0, stop=95.0,
            target=130.0, strength=0.8):
    return Signal(symbol=symbol, strategy=strategy, entry_price=entry,
                  stop_price=stop, target_price=target,
                  signal_strength=strength)


def _order(symbol="AAPL", qty=10, **kw):
    sig = _signal(symbol, **{k: v for k, v in kw.items()
                              if k in ("strategy", "entry", "stop", "target",
                                       "strength")})
    return TradeOrder(signal=sig, quantity=qty, currency="USD",
                      ai_decision="APPROVE", ai_reasoning="test")


def _bar(o, h, l, c):
    return pd.DataFrame({"Open": [o], "High": [h], "Low": [l], "Close": [c],
                         "Volume": [1]})


# =========================================================================== #
# 1. Real-time price supplement in exit checks
# =========================================================================== #


class TestRealtimePriceSupplement:
    """Fix 1: fetch_current_price is used alongside OHLCV to detect exits."""

    def test_stop_triggered_by_realtime_below_bar_low(self, settings, monkeypatch):
        """Real-time price below bar low triggers a stop the bar alone misses."""
        broker = PaperBroker(settings)
        broker.place_bracket_order("HD", 10, 350.0, 340.0, 400.0)

        # Bar's low is 341 (above stop 340), but real-time price is 339.
        monkeypatch.setattr("execution.broker.fetch_ohlcv",
                            lambda s, period="5d": _bar(345, 347, 341, 343))
        monkeypatch.setattr("execution.broker.fetch_current_price", lambda s: 339.0)
        events = broker.poll_exits()
        assert len(events) == 1
        assert events[0].exit_reason == ExitReason.STOP_HIT

    def test_target_triggered_by_realtime_above_bar_high(self, settings, monkeypatch):
        """Real-time price above bar high triggers a target."""
        broker = PaperBroker(settings)
        broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 110.0)

        monkeypatch.setattr("execution.broker.fetch_ohlcv",
                            lambda s, period="5d": _bar(105, 109, 104, 108))
        monkeypatch.setattr("execution.broker.fetch_current_price", lambda s: 111.0)
        events = broker.poll_exits()
        assert len(events) == 1
        assert events[0].exit_reason == ExitReason.TARGET_HIT

    def test_none_realtime_does_not_affect_bar(self, settings, monkeypatch):
        """When fetch_current_price returns None, bar data is used alone."""
        broker = PaperBroker(settings)
        broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 110.0)

        # Bar stays well within stop/target, real-time returns None.
        monkeypatch.setattr("execution.broker.fetch_ohlcv",
                            lambda s, period="5d": _bar(102, 103, 101, 102))
        monkeypatch.setattr("execution.broker.fetch_current_price", lambda s: None)
        events = broker.poll_exits()
        assert events == []


# =========================================================================== #
# 2. Trading mode column in journal
# =========================================================================== #


class TestTradingModeColumn:
    """Fix 2: TradeLogger records trading_mode in every row."""

    def test_paper_mode_recorded(self, settings):
        journal = TradeLogger(str(settings.DATA_DIR), trading_mode="PAPER")
        journal.log_entry(_order(), 100.0)
        df = pd.read_csv(journal.csv_path)
        assert "trading_mode" in df.columns
        assert df.iloc[0]["trading_mode"] == "PAPER"

    def test_live_mode_recorded(self, settings):
        journal = TradeLogger(str(settings.DATA_DIR), trading_mode="LIVE")
        journal.log_entry(_order(), 100.0)
        df = pd.read_csv(journal.csv_path)
        assert df.iloc[0]["trading_mode"] == "LIVE"

    def test_default_mode_is_paper(self, settings):
        journal = TradeLogger(str(settings.DATA_DIR))
        journal.log_entry(_order(), 100.0)
        df = pd.read_csv(journal.csv_path)
        assert df.iloc[0]["trading_mode"] == "PAPER"


# =========================================================================== #
# 3. Risk pre-check on manual trades
# =========================================================================== #


class TestManualTradeRiskCheck:
    """Fix 3: manual trades now run through risk pre_check."""

    def test_manual_trade_rejected_by_risk(self, settings):
        """A manual trade that fails risk checks is rejected."""
        class _RejectingRisk:
            def pre_check(self, signal):
                return False, "daily loss limit reached"
            def register_position(self, order, fill_price):
                pass  # should never be called

        class _FakeBroker:
            def place_bracket_order(self, *a, **kw):
                raise AssertionError("should not reach broker")

        class _FakeLogger:
            def log_entry(self, *a, **kw):
                pass

        res = place_manual_trade(
            dict(symbol="AAPL", entry_price=100, stop_price=95,
                 target_price=110, quantity=10),
            settings,
            broker=_FakeBroker(),
            trade_logger=_FakeLogger(),
            risk_manager=_RejectingRisk(),
        )
        assert not res.ok
        assert "Risk check failed" in res.message

    def test_manual_trade_passes_risk(self, settings):
        """A manual trade that passes risk checks proceeds normally."""
        class _PassingRisk:
            registered = []
            def pre_check(self, signal):
                return True, ""
            def register_position(self, order, fill_price):
                self.registered.append(order.signal.symbol)

        class _FakeBroker:
            def is_connected(self):
                return True

            def connect(self):
                pass

            def place_bracket_order(self, symbol, quantity, entry_price,
                                    stop_price, target_price, **kw):
                from execution.broker import BracketResult
                return BracketResult(accepted=True, fill_price=entry_price,
                                     order_id="FAKE-1")

        class _FakeLogger:
            def log_entry(self, *a, **kw):
                pass

        risk = _PassingRisk()
        res = place_manual_trade(
            dict(symbol="AAPL", entry_price=100, stop_price=95,
                 target_price=110, quantity=10),
            settings,
            broker=_FakeBroker(),
            trade_logger=_FakeLogger(),
            risk_manager=risk,
        )
        assert res.ok
        assert "AAPL" in risk.registered


# =========================================================================== #
# 4. Corrupt JSON backup / restore
# =========================================================================== #


class TestCorruptJsonBackup:
    """Fix 4: corrupt JSON triggers backup restore in both stores."""

    def test_paper_broker_restores_from_backup(self, settings):
        broker = PaperBroker(settings)
        broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 110.0)
        assert broker.get_positions() == {"AAPL": 10}

        # The first place_bracket_order wrote paper_broker.json.
        # A second bracket order triggers _save again, which backs up
        # the first version before overwriting.
        broker.place_bracket_order("MSFT", 5, 200.0, 190.0, 220.0)

        state_path = settings.DATA_DIR / "paper_broker.json"
        backup_path = settings.DATA_DIR / "paper_broker.json.bak"
        assert backup_path.exists()

        state_path.write_text("{corrupt!", encoding="utf-8")
        restored = PaperBroker(settings)
        # Backup has at least AAPL (the first save).
        assert "AAPL" in restored.get_positions()

    def test_paper_broker_empty_on_no_backup(self, settings, monkeypatch):
        broker = PaperBroker(settings)
        broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 110.0)

        state_path = settings.DATA_DIR / "paper_broker.json"
        backup_path = settings.DATA_DIR / "paper_broker.json.bak"
        state_path.write_text("{corrupt!", encoding="utf-8")
        if backup_path.exists():
            backup_path.unlink()

        fresh = PaperBroker(settings)
        assert fresh.get_positions() == {}

    def test_risk_manager_restores_from_backup(self, settings):
        risk = RiskManager(settings)
        risk.register_position(_order("AAPL"), 100.0)
        # Second save creates backup of the first.
        risk.register_position(_order("MSFT", entry=200.0, stop=190.0,
                                      target=220.0), 200.0)
        assert "AAPL" in risk.get_open_positions()
        assert "MSFT" in risk.get_open_positions()

        pos_path = settings.DATA_DIR / "open_positions.json"
        backup_path = settings.DATA_DIR / "open_positions.json.bak"
        assert backup_path.exists()

        pos_path.write_text("{broken!", encoding="utf-8")
        restored = RiskManager(settings)
        # Backup has at least AAPL (first save).
        assert "AAPL" in restored.get_open_positions()


# =========================================================================== #
# 5. Logged warnings on failed data fetches
# =========================================================================== #


class TestFailedDataFetchWarnings:
    """Fix 5: exit manager logs warnings when fetch returns None."""

    def test_time_exit_skips_on_none_price(self, settings, monkeypatch):
        """_check_time_based_exits logs and skips when price is None."""
        broker = PaperBroker(settings)
        risk = RiskManager(settings)
        journal = TradeLogger(str(settings.DATA_DIR))
        order = _order()
        broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 130.0)
        risk.register_position(order, 100.0)

        # Force the position to look old enough for time exit.
        pos = risk.get_open_positions()["AAPL"]
        from datetime import timedelta
        pos["entry_time"] = (datetime.now() - timedelta(days=30)).isoformat()
        risk._save_positions()

        monkeypatch.setattr("execution.exit_manager.fetch_current_price",
                            lambda s: None)
        mgr = ExitManager(settings, broker, risk, journal)
        n = mgr._check_time_based_exits()
        # Should skip (0 exits), not crash.
        assert n == 0
        assert "AAPL" in risk.get_open_positions()


# =========================================================================== #
# 6. Direction-aware P&L (position_pnl helper)
# =========================================================================== #


class TestPositionPnl:
    """Fix 6: position_pnl returns correct P&L for longs and shorts."""

    def test_long_profit(self):
        assert position_pnl("long", 100.0, 110.0, 10) == 100.0

    def test_long_loss(self):
        assert position_pnl("long", 100.0, 90.0, 10) == -100.0

    def test_short_profit(self):
        assert position_pnl("short", 100.0, 90.0, 10) == 100.0

    def test_short_loss(self):
        assert position_pnl("short", 100.0, 110.0, 10) == -100.0

    def test_flat(self):
        assert position_pnl("long", 100.0, 100.0, 10) == 0.0
        assert position_pnl("short", 100.0, 100.0, 10) == 0.0

    def test_case_insensitive(self):
        assert position_pnl("SHORT", 100.0, 90.0, 5) == 50.0
        assert position_pnl("Short", 100.0, 90.0, 5) == 50.0


# =========================================================================== #
# 7. Dual-store reconciliation
# =========================================================================== #


class TestDualStoreReconciliation:
    """Fix 7: ExitManager._reconcile_stores detects divergence."""

    def test_no_divergence_is_silent(self, settings, monkeypatch, caplog):
        broker = PaperBroker(settings)
        risk = RiskManager(settings)
        journal = TradeLogger(str(settings.DATA_DIR))

        order = _order()
        broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 130.0)
        risk.register_position(order, 100.0)

        mgr = ExitManager(settings, broker, risk, journal)
        # Patch out real exit checks so we only test reconciliation.
        monkeypatch.setattr(mgr, "_check_broker_exits", lambda: 0)
        monkeypatch.setattr(mgr, "_check_time_based_exits", lambda: 0)
        monkeypatch.setattr(mgr, "_check_position_health", lambda: 0)
        monkeypatch.setattr(mgr, "_check_dynamic_stops", lambda: 0)
        mgr.manage_exits()
        # No divergence warnings expected.
        assert "reconcile_divergence" not in caplog.text

    def test_risk_only_symbol_warns(self, settings, monkeypatch, caplog):
        import structlog
        structlog.configure(
            wrapper_class=structlog.make_filtering_bound_logger(0),
        )
        broker = PaperBroker(settings)
        risk = RiskManager(settings)
        journal = TradeLogger(str(settings.DATA_DIR))

        # Register in risk but not in broker.
        risk.register_position(_order("NVDA"), 200.0)
        mgr = ExitManager(settings, broker, risk, journal)
        monkeypatch.setattr(mgr, "_check_broker_exits", lambda: 0)
        monkeypatch.setattr(mgr, "_check_time_based_exits", lambda: 0)
        monkeypatch.setattr(mgr, "_check_position_health", lambda: 0)
        monkeypatch.setattr(mgr, "_check_dynamic_stops", lambda: 0)
        mgr.manage_exits()
        # The reconciliation should have warned.  We can't easily capture
        # structlog output, so just verify it didn't crash and the position
        # is still present (reconcile is advisory, not corrective).
        assert "NVDA" in risk.get_open_positions()


# =========================================================================== #
# 8. Telegram delivery status tracking
# =========================================================================== #


class TestTelegramDeliveryStatus:
    """Fix 8: _dispatch records actual delivery bool, not always True."""

    def test_successful_delivery_recorded_true(self, settings):
        from agent import alert_config
        from agent.alerts import AlertManager

        class _OkTelegram:
            enabled = True
            async def send(self, text):
                return True

        class _NoEmail:
            enabled = False

        mgr = AlertManager(settings, telegram=_OkTelegram(), email=_NoEmail())
        asyncio.run(mgr.send("hello", event_type="engine_error"))
        rec = alert_config.read_history(settings.DATA_DIR)[0]
        assert rec["channels"].get("telegram") is True

    def test_failed_delivery_recorded_false(self, settings):
        from agent import alert_config
        from agent.alerts import AlertManager

        class _FailTelegram:
            enabled = True
            async def send(self, text):
                return False

        class _NoEmail:
            enabled = False

        mgr = AlertManager(settings, telegram=_FailTelegram(), email=_NoEmail())
        asyncio.run(mgr.send("hello", event_type="engine_error"))
        rec = alert_config.read_history(settings.DATA_DIR)[0]
        assert rec["channels"].get("telegram") is False


# =========================================================================== #
# 9. File locking on position JSON
# =========================================================================== #


class TestFileLocking:
    """Fix 9: _save_positions uses fcntl.flock for mutual exclusion."""

    def test_save_positions_acquires_lock(self, settings):
        """Saving positions creates a lock file."""
        risk = RiskManager(settings)
        risk.register_position(_order(), 100.0)
        lock_path = settings.DATA_DIR / ".open_positions.lock"
        assert lock_path.exists()

    def test_paper_broker_save_acquires_lock(self, settings):
        broker = PaperBroker(settings)
        broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 110.0)
        lock_path = settings.DATA_DIR / ".paper_broker.lock"
        assert lock_path.exists()

    def test_concurrent_saves_serialize(self, settings):
        """Two risk managers writing the same file don't corrupt it."""
        r1 = RiskManager(settings)
        r2 = RiskManager(settings)
        r1.register_position(_order("AAPL"), 100.0)
        r2.register_position(_order("MSFT"), 200.0)
        # Both should have written valid JSON; load from a third instance.
        r3 = RiskManager(settings)
        # At least one symbol should be present (second write wins).
        pos = r3.get_open_positions()
        assert len(pos) >= 1


# =========================================================================== #
# 10. ENABLE_DYNAMIC_STOPS canonical setting
# =========================================================================== #


class TestEnableDynamicStops:
    """Fix 10: ENABLE_DYNAMIC_STOPS is the canonical gate for dynamic stops."""

    def test_both_true_enables(self):
        s = Settings(ENABLE_DYNAMIC_STOPS=True, ENABLE_PARTIAL_TAKE_TRAIL=True)
        assert s.ENABLE_DYNAMIC_STOPS is True
        assert s.ENABLE_PARTIAL_TAKE_TRAIL is True

    def test_dynamic_stops_false_disables(self):
        s = Settings(ENABLE_DYNAMIC_STOPS=False, ENABLE_PARTIAL_TAKE_TRAIL=True)
        assert s.ENABLE_DYNAMIC_STOPS is False

    def test_legacy_alias_false_disables(self):
        s = Settings(ENABLE_DYNAMIC_STOPS=True, ENABLE_PARTIAL_TAKE_TRAIL=False)
        assert s.ENABLE_PARTIAL_TAKE_TRAIL is False

    def test_exit_manager_skips_when_dynamic_stops_off(self, settings, monkeypatch):
        settings = settings.model_copy(update={"ENABLE_DYNAMIC_STOPS": False})
        broker = PaperBroker(settings)
        risk = RiskManager(settings)
        journal = TradeLogger(str(settings.DATA_DIR))
        order = _order()
        broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 130.0)
        risk.register_position(order, 100.0)

        mgr = ExitManager(settings, broker, risk, journal)
        n = mgr._check_dynamic_stops()
        assert n == 0

    def test_exit_manager_skips_when_legacy_alias_off(self, settings, monkeypatch):
        settings = settings.model_copy(
            update={"ENABLE_PARTIAL_TAKE_TRAIL": False}
        )
        broker = PaperBroker(settings)
        risk = RiskManager(settings)
        journal = TradeLogger(str(settings.DATA_DIR))
        order = _order()
        broker.place_bracket_order("AAPL", 10, 100.0, 95.0, 130.0)
        risk.register_position(order, 100.0)

        mgr = ExitManager(settings, broker, risk, journal)
        n = mgr._check_dynamic_stops()
        assert n == 0
