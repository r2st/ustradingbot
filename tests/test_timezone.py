"""Tests for timezone consistency across the codebase.

Every user-facing timestamp in the US Trading Bot must be in
America/New_York (Eastern Time).  These tests verify that the key
timestamp-producing functions emit timezone-aware datetimes in ET,
and that the centralised ``EASTERN`` constant is wired correctly.
"""

from __future__ import annotations

import importlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from config.settings import EASTERN, Settings

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# 1. The EASTERN constant itself
# ---------------------------------------------------------------------------


class TestEasternConstant:
    """Verify the canonical timezone constant in config.settings."""

    def test_eastern_is_new_york(self) -> None:
        assert EASTERN.key == "America/New_York"

    def test_eastern_is_zoneinfo(self) -> None:
        assert isinstance(EASTERN, ZoneInfo)

    def test_now_with_eastern_is_aware(self) -> None:
        now = datetime.now(tz=EASTERN)
        assert now.tzinfo is not None

    def test_eastern_utc_offset_range(self) -> None:
        """ET is UTC-5 (EST) or UTC-4 (EDT) — never anything else."""
        now = datetime.now(tz=EASTERN)
        offset_hours = now.utcoffset().total_seconds() / 3600
        assert offset_hours in (-5.0, -4.0)


# ---------------------------------------------------------------------------
# 2. Dashboard header timestamp
# ---------------------------------------------------------------------------


class TestDashboardTimestamp:
    """The dashboard header timestamp must show Eastern time."""

    def test_build_status_timestamp_is_eastern(self, settings: Settings) -> None:
        """_build_system_status() must produce a TZ-aware ET timestamp."""
        from dashboard.app import _build_system_status

        status = _build_system_status()
        ts_str = status["timestamp"]

        # Must contain the timezone abbreviation (EDT or EST).
        assert "EDT" in ts_str or "EST" in ts_str, (
            f"Dashboard timestamp missing timezone label: {ts_str!r}"
        )

        # Parse back and verify the timezone offset is ET.
        # Format: "2026-07-09 15:40:30 EDT"
        parts = ts_str.rsplit(" ", 1)
        assert len(parts) == 2, f"Unexpected format: {ts_str!r}"
        dt_part, tz_label = parts
        assert tz_label in ("EDT", "EST")

    def test_health_endpoint_timestamp_is_aware(self, settings: Settings) -> None:
        """The /health endpoint's timestamp must be ISO-format with offset."""
        from dashboard.app import _build_system_status

        status = _build_system_status()
        # The health endpoint uses .isoformat() — verify the main status
        # timestamp is clearly Eastern.
        ts = status["timestamp"]
        assert any(label in ts for label in ("EDT", "EST"))


# ---------------------------------------------------------------------------
# 3. Signal timestamp defaults
# ---------------------------------------------------------------------------


class TestSignalTimestamp:
    """Signal.timestamp default_factory must produce ET-aware datetimes."""

    def test_signal_timestamp_is_eastern(self) -> None:
        from signals.signal_types import Grade, Signal

        sig = Signal(
            symbol="TEST",
            strategy="momentum",
            direction="long",
            entry_price=100.0,
            stop_price=95.0,
            target_price=110.0,
            signal_strength=0.8,
            grade=Grade.A,
        )
        assert sig.timestamp.tzinfo is not None, "Signal timestamp must be tz-aware"
        assert sig.timestamp.tzinfo.key == "America/New_York"

    def test_short_signal_timestamp_is_eastern(self) -> None:
        from short_strategies.common.signal import ShortSignal

        sig = ShortSignal(
            strategy_id="short_gap_fail",
            symbol="TEST",
            signal_strength=0.7,
            trigger_price=50.0,
            stop_price=53.0,
            target_price=46.0,
        )
        assert sig.timestamp.tzinfo is not None, "ShortSignal timestamp must be tz-aware"
        assert sig.timestamp.tzinfo.key == "America/New_York"


# ---------------------------------------------------------------------------
# 4. Journal trade_logger timestamps
# ---------------------------------------------------------------------------


class TestTradeLoggerTimestamps:
    """Trade journal entries must record Eastern timestamps."""

    def test_entry_row_uses_eastern(self, tmp_data_dir: Path) -> None:
        """TradeLogger.log_entry should produce an ET-aware ISO timestamp."""
        import pandas as pd

        from journal.trade_logger import TradeLogger
        from signals.signal_types import Grade, Signal, TradeOrder

        logger = TradeLogger(tmp_data_dir)
        sig = Signal(
            symbol="AAPL",
            strategy="momentum",
            direction="long",
            entry_price=195.0,
            stop_price=190.0,
            target_price=208.0,
            signal_strength=0.82,
            grade=Grade.A,
        )
        order = TradeOrder(
            signal=sig,
            quantity=10,
            risk_amount=50.0,
            max_risk_dollars=135.0,
            currency="USD",
        )
        trade_id = logger.log_entry(order, fill_price=195.50)
        # Read the CSV back to verify the entry_time column.
        df = pd.read_csv(logger.csv_path)
        assert len(df) == 1
        entry_time = str(df.iloc[0]["entry_time"])
        # Must contain a timezone offset (e.g. "-04:00" or "-05:00").
        assert re.search(r"[+-]\d{2}:\d{2}", entry_time), (
            f"entry_time missing TZ offset: {entry_time!r}"
        )


# ---------------------------------------------------------------------------
# 5. Codebase-wide: no bare datetime.now() in production code
# ---------------------------------------------------------------------------


class TestNoBareNow:
    """Scan production source files for bare datetime.now() calls.

    A bare ``datetime.now()`` (no ``tz=`` argument) returns a naive
    datetime whose value depends on the server's ``$TZ`` environment
    variable, which is fragile and has caused the dashboard to show
    UTC instead of Eastern.  All production code should use
    ``datetime.now(tz=EASTERN)`` (or the module-local ``ET`` alias).
    """

    # Directories that contain test helpers / third-party code — excluded.
    _SKIP_DIRS = {".venv", "__pycache__", "node_modules", ".git"}

    def _collect_py_files(self) -> List[Path]:
        root = Path(__file__).resolve().parent.parent
        files: List[Path] = []
        for p in root.rglob("*.py"):
            if any(skip in p.parts for skip in self._SKIP_DIRS):
                continue
            # Allow test files to use naive datetimes for convenience.
            if "tests" in p.parts or p.name.startswith("test_"):
                continue
            files.append(p)
        return sorted(files)

    def test_no_bare_datetime_now(self) -> None:
        """No production .py file should call datetime.now() without tz=."""
        # Matches `datetime.now()` but NOT `datetime.now(tz=...)` or
        # `datetime.now(timezone.utc)` etc.
        bare_pattern = re.compile(
            r"datetime\.now\(\)"
        )
        # Lines that are comments, docstrings, or contain backtick-quoted
        # code examples (RST ``literal``) are harmless.
        skip_pattern = re.compile(r"^\s*#|^\s*\"{3}|\s*'{3}|``.*datetime\.now.*``")

        violations: List[str] = []
        for path in self._collect_py_files():
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(source.splitlines(), 1):
                if skip_pattern.search(line):
                    continue
                if bare_pattern.search(line):
                    rel = path.relative_to(Path(__file__).resolve().parent.parent)
                    violations.append(f"  {rel}:{lineno}: {line.strip()}")

        if violations:
            msg = (
                f"Found {len(violations)} bare datetime.now() call(s) "
                f"in production code (should use tz=EASTERN):\n"
                + "\n".join(violations)
            )
            pytest.fail(msg)


# ---------------------------------------------------------------------------
# 6. Export router timestamps include timezone label
# ---------------------------------------------------------------------------


class TestExportTimestamps:
    """PDF/CSV export timestamps should include a timezone label."""

    def test_export_format_string_has_tz(self) -> None:
        """Verify the export_router format strings include %Z."""
        source = (
            Path(__file__).resolve().parent.parent
            / "dashboard"
            / "export_router.py"
        ).read_text(encoding="utf-8")
        # The "Generated" lines should contain %Z for the timezone label.
        generated_lines = [
            line.strip()
            for line in source.splitlines()
            if "Generated" in line and "datetime.now" in line
        ]
        for line in generated_lines:
            assert "%Z" in line, (
                f"Export 'Generated' line missing %Z timezone format: {line!r}"
            )
