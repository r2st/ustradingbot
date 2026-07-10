"""
Trade journal -- CSV-based log of every entry and exit.

The journal is append-only for entries and update-in-place for exits.
It tracks 37 columns covering signal metadata, AI decisions, execution
details, and post-trade analytics.

The CSV file (``trades.csv``) lives in ``settings.DATA_DIR`` and can be
opened directly in Excel or loaded as a pandas DataFrame for analysis.
"""

from __future__ import annotations

import csv
import io
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from config.settings import EASTERN

import pandas as pd
import structlog

from signals.signal_types import ExitEvent, TradeOrder


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Schema definition -- 37 columns
# ---------------------------------------------------------------------------

SCHEMA_COLUMNS: List[str] = [
    # -- identifiers --
    "trade_id",
    "symbol",
    "strategy",
    "direction",
    "trading_mode",  # "PAPER" or "LIVE" — records the bot's mode at entry time
    # -- signal metadata --
    "signal_strength",
    "grade",
    "rsi_value",
    "rsi_score",
    "macd_histogram",
    "macd_score",
    "ema_score",
    "volume_ratio",
    "volume_score",
    "ripster_score",
    "obv_confirming",
    # -- AI layer --
    "ai_decision",
    "ai_reasoning",
    "ai_cost_usd",
    # -- sizing --
    "quantity",
    "risk_amount",
    "max_risk_dollars",
    "currency",
    # -- entry execution --
    "entry_fill_price",
    "entry_time",
    "entry_commission",
    # -- stop / target --
    "stop_price",
    "target_price",
    # -- exit execution --
    "exit_price",
    "exit_time",
    "exit_reason",
    "exit_commission",
    # -- P&L --
    "pnl_gross",
    "pnl_net",
    "pnl_pct",
    # -- analytics --
    "hold_duration_hours",
    "capture_ratio",
    "r_multiple",
]

_NUM_COLUMNS = len(SCHEMA_COLUMNS)
assert _NUM_COLUMNS == 38, f"Expected 38 columns, got {_NUM_COLUMNS}"


class TradeLogger:
    """Append-only trade journal backed by a CSV file.

    Each entry is written as a new row when a position is opened.
    When the position is closed, the matching row is updated in place
    with exit data and computed analytics.

    Attributes:
        csv_path: Absolute path to the ``trades.csv`` file.
    """

    def __init__(self, data_dir: str, trading_mode: str = "PAPER") -> None:
        """Initialise the trade logger.

        Creates the CSV file with headers if it does not exist.

        Args:
            data_dir: Directory where ``trades.csv`` will be stored.
            trading_mode: ``"PAPER"`` or ``"LIVE"`` — recorded per trade so
                paper and live results can be distinguished in analytics.
        """
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self.csv_path: Path = self._data_dir / "trades.csv"
        self._trading_mode = trading_mode
        self._log = log.bind(component="TradeLogger")

        if not self.csv_path.exists():
            self._write_header()
            self._log.info("trade_logger.created_csv", path=str(self.csv_path))
        else:
            self._migrate_header_if_needed()

        self._trade_counter: int = self._get_next_trade_id()

    # ----------------------------------------------------------- log_entry

    def log_entry(
        self,
        order: TradeOrder,
        fill_price: float,
        commission: float = 0.0,
        quantity: Optional[int] = None,
    ) -> None:
        """Append an entry row to the trade journal.

        Args:
            order: The executed ``TradeOrder``.
            fill_price: Actual broker fill price.
            commission: Entry commission charged by the broker.
            quantity: Optional share count for this fill, overriding
                ``order.quantity`` (used for a scale-in tranche fill).
        """
        signal = order.signal
        qty = int(order.quantity if quantity is None else quantity)
        trade_id = self._trade_counter
        self._trade_counter += 1

        row = {
            "trade_id": trade_id,
            "symbol": signal.symbol,
            "strategy": signal.strategy,
            "direction": signal.direction,
            "trading_mode": self._trading_mode,
            "signal_strength": round(signal.signal_strength, 4),
            "grade": signal.grade.value,
            "rsi_value": round(signal.rsi_value, 2),
            "rsi_score": round(signal.rsi_score, 4),
            "macd_histogram": round(signal.macd_histogram, 6),
            "macd_score": round(signal.macd_score, 4),
            "ema_score": round(signal.ema_score, 4),
            "volume_ratio": round(signal.volume_ratio, 4),
            "volume_score": round(signal.volume_score, 4),
            "ripster_score": round(signal.ripster_score, 4),
            "obv_confirming": signal.obv_confirming,
            "ai_decision": order.ai_decision,
            "ai_reasoning": order.ai_reasoning,
            "ai_cost_usd": round(order.ai_cost_usd, 6),
            "quantity": qty,
            "risk_amount": round(order.risk_amount, 2),
            "max_risk_dollars": round(order.max_risk_dollars, 2),
            "currency": order.currency,
            "entry_fill_price": round(fill_price, 4),
            "entry_time": datetime.now(tz=EASTERN).isoformat(),
            "entry_commission": round(commission, 4),
            "stop_price": round(signal.stop_price, 4),
            "target_price": round(signal.target_price, 4),
            # Exit fields left blank until position is closed
            "exit_price": "",
            "exit_time": "",
            "exit_reason": "",
            "exit_commission": "",
            "pnl_gross": "",
            "pnl_net": "",
            "pnl_pct": "",
            "hold_duration_hours": "",
            "capture_ratio": "",
            "r_multiple": "",
        }

        self._append_row(row)
        self._log.info(
            "trade.entry_logged",
            trade_id=trade_id,
            symbol=signal.symbol,
            fill_price=fill_price,
            quantity=order.quantity,
        )

    # ------------------------------------------------------------ log_exit

    def log_exit(
        self,
        symbol: str,
        exit_event: ExitEvent,
        exit_commission: float = 0.0,
    ) -> None:
        """Update the matching entry row with exit data.

        Finds the most recent open trade for *symbol* (one with no
        ``exit_time``) and fills in exit price, reason, P&L, and
        computed analytics.

        Args:
            symbol: Ticker symbol of the closed position.
            exit_event: Exit details (price, reason, P&L).
            exit_commission: Exit commission charged by the broker.
        """
        try:
            df = pd.read_csv(self.csv_path, dtype=str)
        except (FileNotFoundError, pd.errors.EmptyDataError):
            self._log.error("log_exit.csv_read_failed", symbol=symbol)
            return

        # Find the most recent open row for this symbol
        mask = (df["symbol"] == symbol) & (
            df["exit_time"].isna() | (df["exit_time"] == "")
        )
        matching_indices = df.index[mask].tolist()

        if not matching_indices:
            self._log.warning(
                "log_exit.no_open_trade_found", symbol=symbol
            )
            return

        idx = matching_indices[-1]  # most recent open trade

        # Retrieve entry data for P&L calculations
        try:
            entry_fill_price = float(df.at[idx, "entry_fill_price"])
            quantity = int(float(df.at[idx, "quantity"]))
            entry_time_str = df.at[idx, "entry_time"]
            entry_time = datetime.fromisoformat(entry_time_str)
            # Older rows persisted entry_time tz-naive; assume Eastern so the
            # ``exit_time - entry_time`` subtraction below (exit_time is
            # tz-aware) doesn't raise on offset-naive/aware mixing.
            if entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=EASTERN)
            entry_commission = float(df.at[idx, "entry_commission"] or 0)
            stop_price = float(df.at[idx, "stop_price"])
            target_price = float(df.at[idx, "target_price"])
        except (ValueError, TypeError, KeyError) as exc:
            self._log.error(
                "log_exit.parse_error", symbol=symbol, error=str(exc)
            )
            return

        exit_time = exit_event.exit_date or datetime.now(tz=EASTERN)
        exit_price = exit_event.exit_price

        # Manual sell trades journal direction="short"; every automated entry
        # is long.  A short's profit sign is inverted, so compute the per-share
        # move in the direction of the trade.
        direction = str(df.at[idx, "direction"] or "long").strip().lower() \
            if "direction" in df.columns else "long"
        sign = -1.0 if direction == "short" else 1.0

        # P&L calculations
        actual_move = (exit_price - entry_fill_price) * sign
        pnl_gross = actual_move * quantity
        pnl_net = pnl_gross - entry_commission - exit_commission
        pnl_pct = (
            actual_move / entry_fill_price * 100
            if entry_fill_price > 0
            else 0.0
        )

        # Hold duration
        hold_duration = exit_time - entry_time
        hold_duration_hours = round(
            hold_duration.total_seconds() / 3600, 2
        )

        # Capture ratio: actual move / available move
        available_move = (target_price - entry_fill_price) * sign
        capture_ratio = (
            round(actual_move / available_move, 4)
            if available_move > 0
            else 0.0
        )

        # R-multiple: actual P&L per share / risk per share
        risk_per_share = (entry_fill_price - stop_price) * sign
        r_multiple = (
            round(actual_move / risk_per_share, 4)
            if risk_per_share > 0
            else 0.0
        )

        # Update the row.  The frame is read with ``dtype=str`` so every
        # assigned value must be a string (newer pandas string dtypes reject
        # raw floats).
        df.at[idx, "exit_price"] = str(round(exit_price, 4))
        df.at[idx, "exit_time"] = exit_time.isoformat()
        df.at[idx, "exit_reason"] = exit_event.exit_reason.value
        df.at[idx, "exit_commission"] = str(round(exit_commission, 4))
        df.at[idx, "pnl_gross"] = str(round(pnl_gross, 2))
        df.at[idx, "pnl_net"] = str(round(pnl_net, 2))
        df.at[idx, "pnl_pct"] = str(round(pnl_pct, 4))
        df.at[idx, "hold_duration_hours"] = str(hold_duration_hours)
        df.at[idx, "capture_ratio"] = str(capture_ratio)
        df.at[idx, "r_multiple"] = str(r_multiple)

        # Write back atomically
        self._write_dataframe(df)

        self._log.info(
            "trade.exit_logged",
            symbol=symbol,
            exit_price=exit_price,
            exit_reason=exit_event.exit_reason.value,
            pnl_net=round(pnl_net, 2),
            r_multiple=r_multiple,
            hold_hours=hold_duration_hours,
        )

    # ------------------------------------------------------ log_partial_exit

    def log_partial_exit(
        self,
        symbol: str,
        exit_event: ExitEvent,
        exit_commission: float = 0.0,
    ) -> None:
        """Record a partial profit-take as its own closed row.

        Partial-taking sells part of a position and lets the remainder run.
        To keep the journal's one-row-per-lot accounting correct, this:

        1. writes a **new, fully-closed** row for the shares sold (the P&L is
           computed on that slice only), and
        2. shrinks the still-open row's ``quantity`` to the runner size, so the
           eventual final exit is priced on the remaining shares only.

        The number of shares taken comes from
        ``exit_event.fill_details["quantity"]``.
        """
        try:
            df = pd.read_csv(self.csv_path, dtype=str)
        except (FileNotFoundError, pd.errors.EmptyDataError):
            self._log.error("log_partial_exit.csv_read_failed", symbol=symbol)
            return

        mask = (df["symbol"] == symbol) & (
            df["exit_time"].isna() | (df["exit_time"] == "")
        )
        matching = df.index[mask].tolist()
        if not matching:
            self._log.warning("log_partial_exit.no_open_trade", symbol=symbol)
            return
        idx = matching[-1]

        try:
            entry_fill_price = float(df.at[idx, "entry_fill_price"])
            open_qty = int(float(df.at[idx, "quantity"]))
            stop_price = float(df.at[idx, "stop_price"])
        except (ValueError, TypeError, KeyError) as exc:
            self._log.error("log_partial_exit.parse_error", symbol=symbol, error=str(exc))
            return

        take_qty = int(exit_event.fill_details.get("quantity", 0) or 0)
        take_qty = max(0, min(take_qty, open_qty))
        if take_qty == 0:
            return
        runner_qty = open_qty - take_qty
        exit_price = exit_event.exit_price
        exit_time = exit_event.exit_date or datetime.now(tz=EASTERN)

        # Short-aware maths (manual sell trades journal direction="short").
        direction = str(df.at[idx, "direction"] or "long").strip().lower() \
            if "direction" in df.columns else "long"
        sign = -1.0 if direction == "short" else 1.0

        actual_move = (exit_price - entry_fill_price) * sign
        pnl_gross = actual_move * take_qty
        pnl_net = pnl_gross - exit_commission
        pnl_pct = (
            actual_move / entry_fill_price * 100
            if entry_fill_price > 0
            else 0.0
        )
        risk_per_share = (entry_fill_price - stop_price) * sign
        r_multiple = (
            round(actual_move / risk_per_share, 4)
            if risk_per_share > 0
            else 0.0
        )

        # Shrink the still-open row to the runner size.
        df.at[idx, "quantity"] = str(runner_qty)

        # Build a closed row for the taken slice, copying signal/entry context.
        partial_row = {col: df.at[idx, col] for col in SCHEMA_COLUMNS if col in df.columns}
        partial_row["trade_id"] = self._trade_counter
        self._trade_counter += 1
        partial_row["quantity"] = str(take_qty)
        partial_row["exit_price"] = str(round(exit_price, 4))
        partial_row["exit_time"] = exit_time.isoformat()
        partial_row["exit_reason"] = exit_event.exit_reason.value
        partial_row["exit_commission"] = str(round(exit_commission, 4))
        partial_row["entry_commission"] = "0.0"  # entry commission stays on the runner row
        partial_row["pnl_gross"] = str(round(pnl_gross, 2))
        partial_row["pnl_net"] = str(round(pnl_net, 2))
        partial_row["pnl_pct"] = str(round(pnl_pct, 4))
        partial_row["r_multiple"] = str(r_multiple)
        partial_row["capture_ratio"] = ""
        partial_row["hold_duration_hours"] = ""

        df = pd.concat([df, pd.DataFrame([partial_row])], ignore_index=True)
        self._write_dataframe(df)
        self._log.info(
            "trade.partial_exit_logged",
            symbol=symbol,
            take_qty=take_qty,
            runner_qty=runner_qty,
            exit_price=exit_price,
            pnl_net=round(pnl_net, 2),
        )

    # -------------------------------------------------- query methods

    def get_recent_trades(self, n: int = 20) -> pd.DataFrame:
        """Return the last *n* completed trades.

        A trade is "completed" when it has a non-empty ``exit_time``.

        Args:
            n: Number of most recent completed trades to return.

        Returns:
            DataFrame with up to *n* rows, most recent last.
        """
        try:
            df = pd.read_csv(self.csv_path)
        except (FileNotFoundError, pd.errors.EmptyDataError):
            return pd.DataFrame(columns=SCHEMA_COLUMNS)

        completed = df[df["exit_time"].notna() & (df["exit_time"] != "")]
        return completed.tail(n).reset_index(drop=True)

    def get_capture_ratio_median(self, n: int = 20) -> float:
        """Return the median capture ratio of the last *n* completed trades.

        The capture ratio measures how much of the available move
        (entry to target) was actually captured.  A value of 1.0 means
        the target was hit exactly; > 1.0 means the exit exceeded the
        target; < 0.0 means the trade was a loss.

        Args:
            n: Number of recent completed trades to consider.

        Returns:
            Median capture ratio, or ``0.0`` if there are no completed
            trades.
        """
        recent = self.get_recent_trades(n)
        if recent.empty:
            return 0.0

        ratios = pd.to_numeric(recent["capture_ratio"], errors="coerce")
        ratios = ratios.dropna()
        if ratios.empty:
            return 0.0

        return float(ratios.median())

    def get_daily_pnl(self, date: Optional[datetime] = None) -> float:
        """Return the total net P&L for exits on a given day.

        Args:
            date: The date to query.  Defaults to today.

        Returns:
            Sum of ``pnl_net`` for all trades exited on that date,
            or ``0.0`` if no trades were closed.
        """
        if date is None:
            date = datetime.now(tz=EASTERN)
        target_date = date.date()

        try:
            df = pd.read_csv(self.csv_path, dtype=str)
        except (FileNotFoundError, pd.errors.EmptyDataError):
            return 0.0

        completed = df[df["exit_time"].notna() & (df["exit_time"] != "")]
        if completed.empty:
            return 0.0

        total = 0.0
        for _, row in completed.iterrows():
            try:
                exit_dt = datetime.fromisoformat(str(row["exit_time"]))
                if exit_dt.date() == target_date:
                    pnl = float(row.get("pnl_net", 0) or 0)
                    total += pnl
            except (ValueError, TypeError):
                continue

        return round(total, 2)

    # -------------------------------------------------- private helpers

    def _write_header(self) -> None:
        """Create the CSV file with the header row."""
        try:
            with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=SCHEMA_COLUMNS)
                writer.writeheader()
        except OSError as exc:
            self._log.error(
                "write_header.failed", path=str(self.csv_path), error=str(exc)
            )

    def _migrate_header_if_needed(self) -> None:
        """Normalise an existing CSV whose header predates the current schema.

        ``csv.DictWriter`` appends rows by field *name*, so if the schema gains
        or loses a column between deploys while an old ``trades.csv`` lingers on
        disk, freshly appended rows silently stop matching the on-disk header —
        pandas then raises ``ParserError`` on the mismatched field count and the
        dashboard 500s.  This rewrites the file so header and rows both match
        :data:`SCHEMA_COLUMNS` exactly: columns are re-mapped by name, added
        columns are backfilled (``trading_mode`` with the logger's current mode,
        others blank) and removed columns are dropped.  A no-op when the header
        already matches.
        """
        try:
            with open(self.csv_path, newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, None)
        except OSError as exc:
            self._log.error("migrate_header.read_failed", error=str(exc))
            return

        if header is None or header == SCHEMA_COLUMNS:
            return

        try:
            with open(self.csv_path, newline="", encoding="utf-8") as f:
                old_rows = list(csv.DictReader(f))
        except OSError as exc:
            self._log.error("migrate_header.read_failed", error=str(exc))
            return

        had_mode = "trading_mode" in header
        migrated: List[dict] = []
        for old in old_rows:
            row = {col: (old.get(col) or "") for col in SCHEMA_COLUMNS}
            # Legacy rows carry no trading_mode; assume the current mode.
            if not had_mode:
                row["trading_mode"] = self._trading_mode
            migrated.append(row)

        df = pd.DataFrame(migrated, columns=SCHEMA_COLUMNS)
        self._write_dataframe(df)
        self._log.info(
            "trade_logger.header_migrated",
            path=str(self.csv_path),
            old_columns=len(header),
            new_columns=len(SCHEMA_COLUMNS),
            rows=len(migrated),
        )

    def _append_row(self, row: dict) -> None:
        """Append a single row dict to the CSV file.

        Args:
            row: Dict with keys matching ``SCHEMA_COLUMNS``.
        """
        try:
            with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=SCHEMA_COLUMNS)
                writer.writerow(row)
        except OSError as exc:
            self._log.error(
                "append_row.failed",
                path=str(self.csv_path),
                error=str(exc),
            )

    def _write_dataframe(self, df: pd.DataFrame) -> None:
        """Atomically rewrite the CSV from a DataFrame.

        Writes to a temporary file then renames to prevent corruption.

        Args:
            df: Full trades DataFrame to write.
        """
        import tempfile as _tempfile

        try:
            fd, tmp_path = _tempfile.mkstemp(
                dir=str(self._data_dir),
                prefix=".trades_",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
                    df.to_csv(f, index=False, columns=SCHEMA_COLUMNS)
                os.replace(tmp_path, str(self.csv_path))
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as exc:
            self._log.error(
                "write_dataframe.failed",
                path=str(self.csv_path),
                error=str(exc),
            )

    def _get_next_trade_id(self) -> int:
        """Determine the next trade ID from the existing CSV.

        Returns:
            The next sequential trade ID (max existing + 1), or 1 if
            the CSV is empty.
        """
        try:
            df = pd.read_csv(self.csv_path, usecols=["trade_id"])
            if df.empty:
                return 1
            return int(df["trade_id"].max()) + 1
        except (FileNotFoundError, pd.errors.EmptyDataError, KeyError, ValueError):
            return 1
