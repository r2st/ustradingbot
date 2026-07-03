"""
Earnings calendar module for the US Trading Bot.

Queries Yahoo Finance for upcoming and recent earnings dates.  This feeds
two subsystems:

1. **AI Tier 1 filter** -- If earnings are within 14 days, the signal is
   rejected *before* the Claude API call (saves cost).
2. **PEAD detector** -- Identifies stocks with earnings in the last 1-5
   trading days so the post-earnings-announcement-drift strategy can fire.

All public functions return ``None`` or ``False`` on error -- they never
raise exceptions -- so a broken earnings calendar does not crash the scan
loop.

Typical usage::

    from data.earnings import (
        get_earnings_date,
        is_earnings_within_days,
        get_recent_earnings,
    )

    if is_earnings_within_days("AAPL", days=14):
        # skip -- too close to earnings
        ...

    info = get_recent_earnings("AAPL", lookback_days=5)
    if info is not None:
        # PEAD candidate
        print(info.price_move_pct, info.volume_ratio)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import structlog
import yfinance as yf

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class EarningsInfo:
    """Summary of a recent earnings event for PEAD detection.

    Attributes:
        earnings_date: The date the earnings were reported.
        price_move_pct: Percentage price change on the earnings day
            (close-to-close), e.g. ``5.2`` means +5.2%.
        volume_ratio: Earnings-day volume divided by the 20-day average
            volume *before* earnings.  A ratio > 2.0 indicates heavy
            institutional participation.
        gap_direction: ``"up"`` if the stock gapped up on earnings,
            ``"down"`` if it gapped down, or ``"flat"`` if the gap was
            negligible (< 0.5%).
    """

    earnings_date: datetime
    price_move_pct: float
    volume_ratio: float
    gap_direction: str  # "up" | "down" | "flat"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_earnings_date(symbol: str) -> Optional[datetime]:
    """Return the next upcoming earnings date for *symbol*, or ``None``.

    Queries ``yfinance``'s earnings-dates endpoint which returns the next
    several scheduled earnings dates.  We pick the earliest date that is
    on or after today (UTC).

    Args:
        symbol: Ticker symbol (e.g. ``"AAPL"``).

    Returns:
        A timezone-aware :class:`datetime` of the next earnings, or
        ``None`` if the information is unavailable.
    """
    log = logger.bind(symbol=symbol)

    try:
        ticker = yf.Ticker(symbol)
        earnings_dates = ticker.get_earnings_dates(limit=12)

        if earnings_dates is None or earnings_dates.empty:
            log.debug("get_earnings_date.no_data")
            return None

        now = datetime.now(tz=timezone.utc)

        for date_idx in earnings_dates.index:
            # The index values are Timestamps; make sure they are tz-aware.
            dt = pd.Timestamp(date_idx)
            if dt.tzinfo is None:
                dt = dt.tz_localize("UTC")
            else:
                dt = dt.tz_convert("UTC")

            if dt >= now:
                result = dt.to_pydatetime()
                log.debug("get_earnings_date.found", next_earnings=str(result))
                return result

        log.debug("get_earnings_date.none_upcoming")
        return None

    except Exception:
        log.exception("get_earnings_date.error")
        return None


def is_earnings_within_days(symbol: str, days: int = 14) -> bool:
    """Check whether earnings are within *days* calendar days.

    This is the Tier 1 pre-filter that gates the AI veto call.  If this
    returns ``True``, the signal should be rejected (for non-PEAD
    strategies) to avoid holding through an earnings event.

    Args:
        symbol: Ticker symbol.
        days: Number of calendar days to look ahead.  The system default
            is 14 (from ``SYSTEM_DESIGN.md`` section 8.1).

    Returns:
        ``True`` if earnings fall within the window, ``False`` otherwise
        (including on any error -- fail-open for this filter means the
        AI layer will catch earnings risk if the calendar is broken).
    """
    log = logger.bind(symbol=symbol, days=days)

    try:
        next_date = get_earnings_date(symbol)
        if next_date is None:
            return False

        now = datetime.now(tz=timezone.utc)
        delta = next_date - now

        within = 0 <= delta.total_seconds() <= days * 86_400
        if within:
            log.info(
                "is_earnings_within_days.within_window",
                next_earnings=str(next_date),
                days_until=round(delta.total_seconds() / 86_400, 1),
            )
        return within

    except Exception:
        log.exception("is_earnings_within_days.error")
        return False


def get_recent_earnings(
    symbol: str,
    lookback_days: int = 5,
) -> Optional[EarningsInfo]:
    """Check if earnings occurred within the last *lookback_days* trading
    days and return post-earnings metrics for PEAD detection.

    Retrieves the most recent past earnings date from the yfinance
    calendar, then computes:

    - **Price move**: Close-to-close percentage change on earnings day.
    - **Volume ratio**: Earnings-day volume / 20-day average volume
      (measured over the 20 days *before* the earnings day).
    - **Gap direction**: Whether the stock opened above (``"up"``),
      below (``"down"``), or flat relative to the prior close.

    Args:
        symbol: Ticker symbol.
        lookback_days: How many calendar days to look back for a recent
            earnings event.  Default 5 aligns with the PEAD detector's
            1-5 trading-day window.

    Returns:
        An :class:`EarningsInfo` if a qualifying earnings event was found,
        or ``None`` otherwise.
    """
    log = logger.bind(symbol=symbol, lookback_days=lookback_days)

    try:
        # -- 1. Find the most recent past earnings date -----------------
        ticker = yf.Ticker(symbol)
        earnings_dates = ticker.get_earnings_dates(limit=12)

        if earnings_dates is None or earnings_dates.empty:
            log.debug("get_recent_earnings.no_earnings_data")
            return None

        now = datetime.now(tz=timezone.utc)
        cutoff = now - timedelta(days=lookback_days)
        recent_date: Optional[pd.Timestamp] = None

        for date_idx in sorted(earnings_dates.index, reverse=True):
            dt = pd.Timestamp(date_idx)
            if dt.tzinfo is None:
                dt = dt.tz_localize("UTC")
            else:
                dt = dt.tz_convert("UTC")

            if cutoff <= dt <= now:
                recent_date = dt
                break

        if recent_date is None:
            log.debug("get_recent_earnings.no_recent_event")
            return None

        # -- 2. Fetch OHLCV around the earnings date --------------------
        # We need ~25 trading days before earnings (for 20-day avg volume)
        # plus a few days after.  40 calendar days of padding is safe.
        fetch_start = (recent_date - timedelta(days=40)).strftime("%Y-%m-%d")
        fetch_end = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        hist = ticker.history(start=fetch_start, end=fetch_end, auto_adjust=True)
        if hist is None or hist.empty or len(hist) < 5:
            log.warning(
                "get_recent_earnings.insufficient_history",
                rows=0 if hist is None else len(hist),
            )
            return None

        # Normalise index to tz-aware UTC for comparison.
        if hist.index.tzinfo is None:
            hist.index = hist.index.tz_localize("UTC")
        else:
            hist.index = hist.index.tz_convert("UTC")

        # Find the bar on or closest to the earnings date.
        earnings_date_naive = recent_date.normalize()
        # Get the index position closest to the earnings date.
        idx_distances = abs(hist.index - earnings_date_naive)
        closest_pos = idx_distances.argmin()

        if closest_pos < 1:
            log.warning("get_recent_earnings.earnings_at_start_of_history")
            return None

        # -- 3. Compute metrics -----------------------------------------
        earnings_bar = hist.iloc[closest_pos]
        prev_bar = hist.iloc[closest_pos - 1]

        # Price move: close-to-close percentage change.
        prev_close = float(prev_bar["Close"])
        earnings_close = float(earnings_bar["Close"])
        if prev_close <= 0:
            log.warning("get_recent_earnings.invalid_prev_close")
            return None

        price_move_pct = ((earnings_close - prev_close) / prev_close) * 100.0

        # Volume ratio: earnings-day volume / 20-day average volume
        # (using the 20 bars *before* the earnings bar).
        vol_window_start = max(0, closest_pos - 20)
        avg_volume = float(
            hist.iloc[vol_window_start:closest_pos]["Volume"].mean()
        )
        earnings_volume = float(earnings_bar["Volume"])

        volume_ratio = (earnings_volume / avg_volume) if avg_volume > 0 else 0.0

        # Gap direction: compare earnings-day open to prior close.
        earnings_open = float(earnings_bar["Open"])
        gap_pct = ((earnings_open - prev_close) / prev_close) * 100.0

        if gap_pct > 0.5:
            gap_direction = "up"
        elif gap_pct < -0.5:
            gap_direction = "down"
        else:
            gap_direction = "flat"

        info = EarningsInfo(
            earnings_date=recent_date.to_pydatetime(),
            price_move_pct=round(price_move_pct, 2),
            volume_ratio=round(volume_ratio, 2),
            gap_direction=gap_direction,
        )

        log.info(
            "get_recent_earnings.found",
            earnings_date=str(info.earnings_date),
            price_move_pct=info.price_move_pct,
            volume_ratio=info.volume_ratio,
            gap_direction=info.gap_direction,
        )
        return info

    except Exception:
        log.exception("get_recent_earnings.error")
        return None
