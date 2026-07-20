"""Tests for the economic calendar + macro blackout filter (P1-5)."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from config.settings import EASTERN, Settings
from data.economic_calendar import (
    active_blackout,
    get_events,
    upcoming_events,
)
from signals.macro_filter import MacroEntryFilter


class _Sig:
    def __init__(self, symbol="AAPL", strategy="momentum"):
        self.symbol = symbol
        self.strategy = strategy
        self.raw_data: dict = {}


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


class TestCalendar:
    def test_generates_fomc_and_nfp(self) -> None:
        now = datetime(2025, 1, 1, tzinfo=EASTERN)
        events = get_events(now, lookahead_days=60, lookback_days=0)
        types = {e.event_type for e in events}
        assert "fomc" in types
        assert "nfp" in types
        # NFP is a Friday at 08:30 ET.
        nfp = next(e for e in events if e.event_type == "nfp")
        assert nfp.when.weekday() == 4
        assert nfp.when.strftime("%H:%M") == "08:30"

    def test_fomc_known_date(self) -> None:
        # 2025-01-29 is a curated FOMC decision date at 14:00 ET.
        now = datetime(2025, 1, 1, tzinfo=EASTERN)
        events = get_events(now, lookahead_days=45, lookback_days=0)
        fomc = [e for e in events if e.event_type == "fomc"]
        assert any(e.when.date().isoformat() == "2025-01-29" for e in fomc)

    def test_upcoming_events_have_days_until(self) -> None:
        now = datetime(2025, 1, 1, tzinfo=EASTERN)
        # upcoming_events uses "now" internally; just assert structure on live call.
        rows = upcoming_events(now=now, days=30)
        assert rows and all("days_until" in r and "type" in r for r in rows)

    def test_override_file(self, tmp_path) -> None:
        (tmp_path / "economic_calendar.json").write_text(
            json.dumps({"events": [
                {"date": "2025-02-15", "time": "10:00", "type": "fomc",
                 "name": "Custom FOMC"}
            ]})
        )
        now = datetime(2025, 2, 10, tzinfo=EASTERN)
        events = get_events(now, lookahead_days=10, lookback_days=0,
                            data_dir=tmp_path)
        custom = [e for e in events if e.name == "Custom FOMC"]
        assert custom and custom[0].when.strftime("%H:%M") == "10:00"


# ---------------------------------------------------------------------------
# Blackout window
# ---------------------------------------------------------------------------


class TestBlackout:
    def test_active_inside_window(self) -> None:
        # 2025-01-29 14:00 ET FOMC; 6 hours before -> inside a 24h/12h window.
        now = datetime(2025, 1, 29, 8, 0, tzinfo=EASTERN)
        ev = active_blackout(now=now, hours_before=24, hours_after=12,
                             event_types=["fomc"])
        assert ev is not None and ev.event_type == "fomc"

    def test_inactive_outside_window(self) -> None:
        now = datetime(2025, 1, 20, 10, 0, tzinfo=EASTERN)  # >24h before FOMC
        ev = active_blackout(now=now, hours_before=24, hours_after=12,
                             event_types=["fomc"])
        assert ev is None

    def test_event_type_filter(self) -> None:
        # A moment inside an NFP window but only FOMC requested -> no blackout.
        now = datetime(2025, 1, 1, tzinfo=EASTERN)
        events = get_events(now, lookahead_days=10, lookback_days=0)
        nfp = next(e for e in events if e.event_type == "nfp")
        at_nfp = nfp.when
        assert active_blackout(now=at_nfp, hours_before=1, hours_after=1,
                               event_types=["fomc"]) is None
        assert active_blackout(now=at_nfp, hours_before=1, hours_after=1,
                               event_types=["nfp"]) is not None


# ---------------------------------------------------------------------------
# Macro filter
# ---------------------------------------------------------------------------


class TestMacroFilter:
    def test_disabled_allows(self) -> None:
        s = Settings(MACRO_FILTER_MODE="off")
        f = MacroEntryFilter(s)
        assert f.check(_Sig()).allowed is True

    def test_block_mode_rejects_in_window(self) -> None:
        s = Settings(MACRO_FILTER_MODE="block")
        clock = lambda: datetime(2025, 1, 29, 8, 0, tzinfo=EASTERN)
        f = MacroEntryFilter(s, now_fn=clock)
        sig = _Sig()
        check = f.check(sig)
        assert check.allowed is False
        assert check.mode == "block"
        assert sig.raw_data.get("macro_flag")  # annotated

    def test_flag_mode_allows_but_annotates(self) -> None:
        s = Settings(MACRO_FILTER_MODE="flag")
        clock = lambda: datetime(2025, 1, 29, 8, 0, tzinfo=EASTERN)
        f = MacroEntryFilter(s, now_fn=clock)
        sig = _Sig()
        check = f.check(sig)
        assert check.allowed is True
        assert check.mode == "flag"
        assert sig.raw_data.get("macro_flag")

    def test_outside_window_allows(self) -> None:
        s = Settings(MACRO_FILTER_MODE="block")
        clock = lambda: datetime(2025, 1, 20, 10, 0, tzinfo=EASTERN)
        f = MacroEntryFilter(s, now_fn=clock)
        assert f.check(_Sig()).allowed is True

    def test_fail_open_on_fetcher_error(self) -> None:
        s = Settings(MACRO_FILTER_MODE="block")

        def _boom(*a, **k):
            raise RuntimeError("calendar down")

        f = MacroEntryFilter(s, blackout_fetcher=_boom)
        assert f.check(_Sig()).allowed is True
