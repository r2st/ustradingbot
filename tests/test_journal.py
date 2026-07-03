"""Tests for trade journal and rejected signal logger."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from journal.trade_logger import TradeLogger, SCHEMA_COLUMNS
from journal.btst_logger import RejectedSignalLogger
from signals.signal_types import ExitEvent, ExitReason, Grade, Signal, TradeOrder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_order() -> TradeOrder:
    """Create a sample trade order for testing."""
    signal = Signal(
        symbol="AAPL",
        strategy="momentum",
        entry_price=195.50,
        stop_price=190.20,
        target_price=208.45,
        signal_strength=0.82,
        grade=Grade.A,
        rsi_value=62.3,
        rsi_score=0.80,
        macd_histogram=0.45,
        macd_score=0.85,
        ema_score=0.90,
        volume_ratio=2.1,
        volume_score=0.75,
        ripster_score=0.80,
        obv_confirming=True,
        timestamp=datetime.now(),
    )
    return TradeOrder(
        signal=signal,
        quantity=15,
        risk_amount=79.50,
        max_risk_dollars=135.00,
        currency="USD",
        ai_decision="APPROVE",
        ai_reasoning="No negative news",
        ai_cost_usd=0.023,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Trade Logger Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestTradeLogger:
    """Tests for the CSV trade journal."""

    def test_creates_csv_with_headers(self, tmp_data_dir: Path) -> None:
        """Initialising the logger should create a CSV with schema headers."""
        logger = TradeLogger(str(tmp_data_dir))
        csv_path = tmp_data_dir / "trades.csv"
        assert csv_path.exists()

        content = csv_path.read_text()
        # Verify at least the first few schema columns are present
        assert "trade_id" in content or "symbol" in content

    def test_log_entry(self, tmp_data_dir: Path) -> None:
        """Logging an entry should append a row to the CSV."""
        logger = TradeLogger(str(tmp_data_dir))
        order = _make_order()
        logger.log_entry(order, fill_price=195.60, commission=1.00)

        csv_path = tmp_data_dir / "trades.csv"
        content = csv_path.read_text()
        assert "AAPL" in content
        assert "momentum" in content

    def test_log_multiple_entries(self, tmp_data_dir: Path) -> None:
        """Multiple entries should all be recorded."""
        logger = TradeLogger(str(tmp_data_dir))

        for sym in ["AAPL", "MSFT", "NVDA"]:
            signal = Signal(
                symbol=sym, strategy="momentum",
                entry_price=100.0, stop_price=95.0, target_price=109.0,
                signal_strength=0.80, grade=Grade.A,
                timestamp=datetime.now(),
            )
            order = TradeOrder(
                signal=signal, quantity=10, currency="USD",
                ai_decision="APPROVE", ai_reasoning="ok",
            )
            logger.log_entry(order, fill_price=100.0)

        content = (tmp_data_dir / "trades.csv").read_text()
        assert "AAPL" in content
        assert "MSFT" in content
        assert "NVDA" in content


# ═══════════════════════════════════════════════════════════════════════════
# Rejected Signal Logger Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestRejectedSignalLogger:
    """Tests for the JSONL rejected signal logger."""

    def test_log_rejection(self, tmp_data_dir: Path) -> None:
        """Logging a rejection should write a JSONL line."""
        logger = RejectedSignalLogger(str(tmp_data_dir))
        signal = Signal(
            symbol="TSLA", strategy="momentum",
            entry_price=265.0, stop_price=258.10, target_price=277.42,
            signal_strength=0.75, grade=Grade.B,
            timestamp=datetime.now(),
        )
        logger.log_rejection(signal, reason="AI_REJECT", detail="Lawsuit pending")

        jsonl_path = tmp_data_dir / "rejected_signals.jsonl"
        assert jsonl_path.exists()

        content = jsonl_path.read_text().strip()
        record = json.loads(content)
        assert record["symbol"] == "TSLA"
        assert record["reason"] == "AI_REJECT"

    def test_multiple_rejections(self, tmp_data_dir: Path) -> None:
        """Multiple rejections should each be on their own line."""
        logger = RejectedSignalLogger(str(tmp_data_dir))

        for sym in ["TSLA", "AMD", "COIN"]:
            signal = Signal(
                symbol=sym, strategy="momentum",
                entry_price=100.0, stop_price=95.0, target_price=109.0,
                signal_strength=0.70, grade=Grade.B,
                timestamp=datetime.now(),
            )
            logger.log_rejection(signal, reason="risk_check_failed")

        lines = (tmp_data_dir / "rejected_signals.jsonl").read_text().strip().split("\n")
        assert len(lines) == 3

    def test_get_recent_rejections(self, tmp_data_dir: Path) -> None:
        """Should retrieve the last N rejections."""
        logger = RejectedSignalLogger(str(tmp_data_dir))

        for i in range(10):
            signal = Signal(
                symbol=f"SYM{i}", strategy="momentum",
                entry_price=100.0, stop_price=95.0, target_price=109.0,
                signal_strength=0.70, grade=Grade.B,
                timestamp=datetime.now(),
            )
            logger.log_rejection(signal, reason=f"reason_{i}")

        recent = logger.get_recent_rejections(n=5)
        assert len(recent) == 5
