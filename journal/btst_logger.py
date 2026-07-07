"""
Rejected-signal logger -- JSONL file of signals that did not pass risk checks.

Every signal rejected by the risk manager's ``pre_check`` or
``check_strategy_cap`` is recorded here for later review.  The JSONL
format (one JSON object per line) is efficient for append-only writes
and easy to parse with standard tools or ``pandas.read_json(lines=True)``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import structlog

from signals.signal_types import Signal


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class RejectedSignalLogger:
    """Append-only JSONL logger for signals that were rejected pre-trade.

    Each rejection record includes the full signal snapshot, the rejection
    reason, and an optional detail string.

    Attributes:
        jsonl_path: Absolute path to the ``rejected_signals.jsonl`` file.
    """

    def __init__(self, data_dir: str, on_rejection=None) -> None:
        """Initialise the rejected-signal logger.

        Args:
            data_dir: Directory where ``rejected_signals.jsonl`` will
                be stored.
            on_rejection: Optional ``callback(signal, reason, detail)`` invoked
                (best-effort) after every logged rejection — the engine uses
                this to mirror rejections into the activity feed (monitoring
                F4) so the two logs can never drift apart.
        """
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path: Path = self._data_dir / "rejected_signals.jsonl"
        self._log = log.bind(component="RejectedSignalLogger")
        self._on_rejection = on_rejection

    # --------------------------------------------------------- log_rejection

    def log_rejection(
        self,
        signal: Signal,
        reason: str,
        detail: str = "",
    ) -> None:
        """Append a rejection record to the JSONL file.

        Args:
            signal: The ``Signal`` that was rejected.
            reason: Short machine-readable reason string (e.g.
                ``"daily_loss_limit_reached"``).
            detail: Optional human-readable detail or context.
        """
        record: Dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "symbol": signal.symbol,
            "strategy": signal.strategy,
            "direction": signal.direction,
            "entry_price": signal.entry_price,
            "stop_price": signal.stop_price,
            "target_price": signal.target_price,
            "signal_strength": round(signal.signal_strength, 4),
            "grade": signal.grade.value,
            "rsi_value": round(signal.rsi_value, 2),
            "volume_ratio": round(signal.volume_ratio, 4),
            "reason": reason,
            "detail": detail,
        }

        try:
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except OSError as exc:
            self._log.error(
                "log_rejection.write_failed",
                path=str(self.jsonl_path),
                error=str(exc),
            )
            return

        self._log.debug(
            "signal.rejected",
            symbol=signal.symbol,
            reason=reason,
            strategy=signal.strategy,
        )

        if self._on_rejection is not None:
            try:
                self._on_rejection(signal, reason, detail)
            except Exception:  # noqa: BLE001 -- mirroring must never raise
                self._log.debug("on_rejection_callback_failed", exc_info=True)

    # ----------------------------------------------------- get_recent

    def get_recent_rejections(self, n: int = 50) -> List[Dict[str, Any]]:
        """Return the last *n* rejection records.

        Args:
            n: Maximum number of records to return (most recent last).

        Returns:
            List of dicts, each representing one rejected signal.
            Returns an empty list if the file does not exist or is empty.
        """
        if not self.jsonl_path.exists():
            return []

        records: List[Dict[str, Any]] = []
        try:
            with open(self.jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        self._log.warning(
                            "get_recent_rejections.malformed_line",
                            line_preview=line[:80],
                        )
        except OSError as exc:
            self._log.error(
                "get_recent_rejections.read_failed",
                path=str(self.jsonl_path),
                error=str(exc),
            )
            return []

        return records[-n:]
