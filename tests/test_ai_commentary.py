"""Tests for the AI commentary engine (TA2): templates, sentiment, VIX
bucketing, budget counter, market-hours gating, JSON parsing, and the
/api/ai/* router."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dash
from config.settings import Settings
from ai import llm_router
from dashboard import ai_commentary as ac
from dashboard.ai_commentary import (
    CommentaryEngine,
    derive_market_bias,
    derive_sentiment,
    is_market_open,
    key_levels,
    parse_llm_json,
    template_market_summary,
    template_position_action,
    template_position_commentary,
    template_watchlist_commentary,
    vix_bucket,
)

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------- VIX buckets

@pytest.mark.parametrize(
    "value,expected",
    [
        (10.0, "low"),
        (14.99, "low"),
        (15.0, "normal"),
        (19.9, "normal"),
        (20.0, "elevated"),
        (29.9, "elevated"),
        (30.0, "crisis"),
        (55.0, "crisis"),
    ],
)
def test_vix_bucket(value: float, expected: str) -> None:
    assert vix_bucket(value) == expected


# ------------------------------------------------------------- market hours

def test_market_open_weekday_midday(settings: Settings) -> None:
    # Tuesday 2026-07-07 is a weekday; noon ET is inside regular hours.
    now = datetime(2026, 7, 7, 12, 0, tzinfo=ET)
    assert is_market_open(settings, now) is True


def test_market_closed_weekend_and_after_hours(settings: Settings) -> None:
    saturday = datetime(2026, 7, 11, 12, 0, tzinfo=ET)
    assert is_market_open(settings, saturday) is False
    after_close = datetime(2026, 7, 7, 16, 0, tzinfo=ET)
    assert is_market_open(settings, after_close) is False
    before_open = datetime(2026, 7, 7, 9, 29, tzinfo=ET)
    assert is_market_open(settings, before_open) is False


# ----------------------------------------------------------- LLM JSON parsing

@pytest.mark.parametrize(
    "text,ok",
    [
        ('{"positions": [{"symbol": "AAPL", "commentary": "x"}]}', True),
        ('Sure!\n```json\n{"summary": "calm market"}\n```', True),
        ("no json here at all", False),
        ("", False),
        ('{"broken": ', False),
        ("[1, 2, 3]", False),  # array, not an object
    ],
)
def test_parse_llm_json(text: str, ok: bool) -> None:
    obj = parse_llm_json(text)
    assert (obj is not None) is ok


# ------------------------------------------------------ deterministic prose

def test_template_position_commentary_uses_actual_values() -> None:
    ind = {
        "rsi": 62.0, "rsi_rising": True, "rsi_overbought": False,
        "rsi_momentum_zone": True, "macd_hist": 0.42,
        "macd_hist_expanding": True, "ema20": 365.40, "above_ema20": True,
        "volume_ratio": 1.4, "fast_cloud_bullish": True,
        "slow_cloud_bullish": True,
    }
    text = template_position_commentary(ind)
    assert "RSI at 62" in text
    assert "$365.40" in text
    assert "above" in text
    assert "positive and expanding" in text
    assert "1.4x" in text


def test_template_position_commentary_handles_missing_indicators() -> None:
    text = template_position_commentary(None)
    assert "unavailable" in text.lower()


def test_template_position_action_branches() -> None:
    near_target = {"distance_to_target_pct": 1.0, "distance_to_stop_pct": 8.0,
                   "r_progress": 0.5}
    assert "target" in template_position_action(near_target, None).lower()
    near_stop = {"distance_to_target_pct": 9.0, "distance_to_stop_pct": 0.8,
                 "r_progress": -0.5}
    assert "stop" in template_position_action(near_stop, None).lower()
    past_1r = {"distance_to_target_pct": 5.0, "distance_to_stop_pct": 5.0,
               "r_progress": 1.2}
    assert "breakeven" in template_position_action(past_1r, None).lower()
    weak = {"distance_to_target_pct": 5.0, "distance_to_stop_pct": 5.0,
            "r_progress": 0.1}
    assert "weakening" in template_position_action(
        weak, {"label": "bearish"}).lower()
    hold = template_position_action(weak, {"label": "bullish"})
    assert "hold" in hold.lower()


def test_template_watchlist_commentary_mentions_gate() -> None:
    row = {
        "status": "rejected",
        "last_rejection": {"gate": "ai_veto",
                           "detail": "earnings within 14 days"},
        "signal": None,
    }
    text = template_watchlist_commentary(row, None)
    assert "ai_veto" in text
    assert "earnings within 14 days" in text


def test_template_market_summary_from_facts() -> None:
    market = {
        "spy": {"regime": "bull", "volatility": "normal"},
        "qqq": {"regime": "bull"},
        "vix": {"value": 13.8, "bucket": "low"},
        "sectors": [
            {"name": "Technology", "change_1d": 1.2},
            {"name": "Energy", "change_1d": -0.9},
        ],
        "bias": {"label": "risk-on", "note": "Momentum favored."},
    }
    text = template_market_summary(market)
    assert "bull regime" in text
    assert "VIX 13.8" in text
    assert "Technology" in text and "Energy" in text


def test_derive_market_bias_labels() -> None:
    bull = {"spy": {"regime": "bull", "weight_multipliers": {"momentum": 1.2}},
            "vix": {"bucket": "low"}}
    assert derive_market_bias(bull)["label"] == "risk-on"
    bear = {"spy": {"regime": "bear", "weight_multipliers": {}},
            "vix": {"bucket": "normal"}}
    assert derive_market_bias(bear)["label"] == "risk-off"
    crisis = {"spy": {"regime": "bull", "weight_multipliers": {}},
              "vix": {"bucket": "crisis"}}
    assert derive_market_bias(crisis)["label"] == "risk-off"
    side = {"spy": {"regime": "sideways", "weight_multipliers": {}},
            "vix": {"bucket": "normal"}}
    assert derive_market_bias(side)["label"] == "mixed"


# ------------------------------------------------------- sentiment + levels

def test_sentiment_orders_bullish_above_bearish(bullish_df,
                                                bearish_df) -> None:
    """The chip must rank clearly bullish data above clearly bearish data.

    Note the synthetic bullish fixture trips the OBV-divergence hard-zero on
    the volume component, so its absolute score lands neutral-ish — the
    ordering (and the bearish label on falling data) is the stable contract.
    """
    bull = derive_sentiment(bullish_df, "momentum")
    bear = derive_sentiment(bearish_df, "momentum")
    assert bull is not None and bear is not None
    assert bull["score"] > bear["score"]
    assert bull["label"] in ("bullish", "neutral")
    assert bear["label"] == "bearish"
    for s in (bull, bear):
        assert 0.0 <= s["confidence"] <= 1.0
        assert 0.0 <= s["score"] <= 1.0


def test_sentiment_none_on_short_data(short_df) -> None:
    assert derive_sentiment(short_df, "momentum") is None


def test_compute_indicators_scalars(bullish_df) -> None:
    ind = ac.compute_indicators(bullish_df)
    assert ind is not None
    assert 0 <= ind["rsi"] <= 100
    assert ind["ema20"] > 0
    assert ind["atr14"] > 0
    assert isinstance(ind["above_ema20"], bool)


def test_key_levels_synthetic_series(bullish_df) -> None:
    levels = key_levels(bullish_df)
    price = float(bullish_df["Close"].iloc[-1])
    for lvl in levels["support"]:
        assert lvl["price"] < price
        assert lvl["touches"] >= 1
    for lvl in levels["resistance"]:
        assert lvl["price"] >= price
    assert len(levels["support"]) <= 2
    assert len(levels["resistance"]) <= 2


def test_key_levels_tiny_frame_is_empty(bullish_df) -> None:
    # Under 30 bars there are not enough pivots to trust — return empty.
    tiny = bullish_df.tail(10)
    assert key_levels(tiny) == {"support": [], "resistance": []}


# --------------------------------------------------------- engine + budget

def _engine(settings: Settings) -> CommentaryEngine:
    return CommentaryEngine(settings)


def test_budget_counter_and_exhaustion(settings: Settings) -> None:
    settings = Settings(DATA_DIR=settings.DATA_DIR,
                        AI_COMMENTARY_MAX_CALLS_PER_DAY=2)
    engine = _engine(settings)
    assert engine.budget_exhausted() is False
    engine._bump_budget()
    engine._bump_budget()
    assert engine._budget()["used"] == 2
    assert engine.budget_exhausted() is True


def test_budget_rolls_over_daily(settings: Settings) -> None:
    engine = _engine(settings)
    yesterday = (datetime.now(tz=ET).date() - timedelta(days=1)).isoformat()
    engine._payload = {"budget": {"date": yesterday, "used": 999}}
    assert engine._budget()["used"] == 0
    assert engine.budget_exhausted() is False


def test_should_refresh_requires_recent_poll(settings: Settings) -> None:
    engine = _engine(settings)
    # Stale + never polled -> suppressed (no browser open, no spend).
    assert engine.should_refresh() is False
    engine.note_poll()
    # No payload yet -> first run allowed even off-hours.
    assert engine.should_refresh() is True


def test_should_refresh_gated_by_market_hours(settings: Settings,
                                              monkeypatch) -> None:
    engine = _engine(settings)
    engine.note_poll()
    old = datetime.now(tz=ET) - timedelta(hours=2)
    engine._payload = {"generated_at": old.isoformat()}
    monkeypatch.setattr(ac, "is_market_open", lambda s, now=None: False)
    assert engine.should_refresh() is False
    monkeypatch.setattr(ac, "is_market_open", lambda s, now=None: True)
    assert engine.should_refresh() is True


def test_fresh_payload_not_refreshed(settings: Settings) -> None:
    engine = _engine(settings)
    engine.note_poll()
    engine._payload = {"generated_at": datetime.now(tz=ET).isoformat()}
    assert engine.is_stale() is False
    assert engine.should_refresh() is False


def test_refresh_falls_back_to_templates_without_llm(settings: Settings,
                                                     monkeypatch) -> None:
    """No API key -> every section renders template prose, zero exceptions."""
    settings = Settings(DATA_DIR=settings.DATA_DIR, OPENROUTER_API_KEY="")
    engine = _engine(settings)

    positions = [{
        "symbol": "NVDA", "strategy": "momentum", "grade": "A",
        "entry_price": 100.0, "stop_price": 95.0, "target_price": 109.0,
        "current_price": 103.0, "unrealized_pnl": 30.0, "unrealized_pct": 3.0,
        "distance_to_stop_pct": 7.8, "distance_to_target_pct": 5.8,
        "r_progress": 0.6, "quantity": 10,
        "indicators": {"rsi": 61.0, "rsi_rising": True, "rsi_overbought": False,
                       "rsi_momentum_zone": True, "macd_hist": 0.2,
                       "macd_hist_expanding": True, "ema20": 99.0,
                       "above_ema20": True, "volume_ratio": 2.1,
                       "fast_cloud_bullish": True, "slow_cloud_bullish": True},
        "sentiment": {"label": "bullish", "score": 0.8, "confidence": 0.7},
        "key_levels": {"support": [], "resistance": []},
    }]
    watchlist = [{
        "symbol": "AAPL", "status": "rejected", "price": 200.0,
        "change_pct": 0.5, "readiness": 42, "signal": None,
        "last_rejection": {"gate": "ai_veto", "detail": "earnings soon",
                           "ts": "2026-07-07T10:00:00"},
        "indicators": None, "sentiment": None,
        "key_levels": {"support": [], "resistance": []},
    }]
    market = {"spy": {"regime": "bull", "volatility": "normal",
                      "weight_multipliers": {"momentum": 1.2, "swing": 0.9}},
              "qqq": {"regime": "bull"},
              "vix": {"value": 14.0, "bucket": "low", "source": "vix"},
              "sectors": []}
    market["bias"] = derive_market_bias(market)

    monkeypatch.setattr(ac, "build_position_facts", lambda s: positions)
    monkeypatch.setattr(ac, "build_watchlist_facts", lambda s: watchlist)
    monkeypatch.setattr(ac, "build_market_facts", lambda s: market)

    payload = asyncio.run(engine.refresh(force=True))

    assert payload["positions"][0]["source"] == "template"
    assert "RSI at 61" in payload["positions"][0]["commentary"]
    assert payload["positions"][0]["action"]
    assert payload["watchlist"][0]["source"] == "template"
    assert "ai_veto" in payload["watchlist"][0]["commentary"]
    assert payload["market"]["source"] == "template"
    assert payload["market"]["summary"]
    assert payload["budget"]["used"] == 0  # no LLM calls attempted
    # Persisted to DATA_DIR/ai_commentary.json.
    on_disk = json.loads(
        (settings.DATA_DIR / "ai_commentary.json").read_text())
    assert on_disk["positions"][0]["symbol"] == "NVDA"


def test_refresh_uses_llm_prose_when_available(settings: Settings,
                                               monkeypatch) -> None:
    settings = Settings(DATA_DIR=settings.DATA_DIR,
                        OPENROUTER_API_KEY="test-key")
    engine = _engine(settings)

    positions = [{"symbol": "NVDA", "strategy": "momentum",
                  "entry_price": 100.0, "stop_price": 95.0,
                  "target_price": 109.0, "current_price": 103.0,
                  "indicators": None, "sentiment": None,
                  "key_levels": {"support": [], "resistance": []}}]
    monkeypatch.setattr(ac, "build_position_facts", lambda s: positions)
    monkeypatch.setattr(ac, "build_watchlist_facts", lambda s: [])
    monkeypatch.setattr(
        ac, "build_market_facts",
        lambda s: {"spy": {}, "qqq": {}, "vix": {}, "sectors": [],
                   "bias": {"label": "mixed", "note": ""}})

    responses = {
        "positions": '{"positions": [{"symbol": "NVDA", '
                     '"commentary": "Price is testing the 50 EMA.", '
                     '"action": "Hold."}]}',
        "summary": '{"summary": "Calm bullish tape."}',
    }

    async def fake_call(prompt: str):
        engine._bump_budget()  # mirror the real call's budget accounting
        return responses["positions"] if "open position" in prompt \
            else responses["summary"]

    monkeypatch.setattr(engine, "_call_llm", fake_call)
    payload = asyncio.run(engine.refresh(force=True))

    assert payload["positions"][0]["source"] == "llm"
    assert payload["positions"][0]["commentary"] == "Price is testing the 50 EMA."
    assert payload["market"]["source"] == "llm"
    assert payload["budget"]["used"] == 2  # positions + market (no watchlist)


def test_refresh_budget_exhausted_skips_llm(settings: Settings,
                                            monkeypatch) -> None:
    settings = Settings(DATA_DIR=settings.DATA_DIR,
                        OPENROUTER_API_KEY="test-key",
                        AI_COMMENTARY_MAX_CALLS_PER_DAY=0)
    engine = _engine(settings)
    monkeypatch.setattr(ac, "build_position_facts", lambda s: [])
    monkeypatch.setattr(ac, "build_watchlist_facts", lambda s: [])
    monkeypatch.setattr(
        ac, "build_market_facts",
        lambda s: {"spy": {}, "qqq": {}, "vix": {}, "sectors": [],
                   "bias": {"label": "mixed", "note": ""}})

    called = []

    async def fake_call(prompt: str):
        called.append(prompt)
        return '{"summary": "x"}'

    monkeypatch.setattr(engine, "_call_llm", fake_call)
    payload = asyncio.run(engine.refresh(force=True))
    assert called == []
    assert payload["market"]["source"] == "template"


def test_malformed_llm_response_falls_back(settings: Settings,
                                           monkeypatch) -> None:
    settings = Settings(DATA_DIR=settings.DATA_DIR,
                        OPENROUTER_API_KEY="test-key")
    engine = _engine(settings)
    positions = [{"symbol": "NVDA", "strategy": "momentum",
                  "entry_price": 100.0, "stop_price": 95.0,
                  "target_price": 109.0, "indicators": None,
                  "sentiment": None,
                  "key_levels": {"support": [], "resistance": []}}]
    monkeypatch.setattr(ac, "build_position_facts", lambda s: positions)
    monkeypatch.setattr(ac, "build_watchlist_facts", lambda s: [])
    monkeypatch.setattr(
        ac, "build_market_facts",
        lambda s: {"spy": {}, "qqq": {}, "vix": {}, "sectors": [],
                   "bias": {"label": "mixed", "note": ""}})

    async def bad_call(prompt: str):
        return "I am not JSON at all"

    monkeypatch.setattr(engine, "_call_llm", bad_call)
    payload = asyncio.run(engine.refresh(force=True))
    assert payload["positions"][0]["source"] == "template"
    assert payload["market"]["source"] == "template"


# --------------------------------------------------------- analyst cards


def test_refresh_payload_includes_cards(settings: Settings,
                                        monkeypatch) -> None:
    """Every row carries the UX-spec card structure; the shared term help
    ships once at payload level."""
    settings = Settings(DATA_DIR=settings.DATA_DIR, OPENROUTER_API_KEY="")
    engine = _engine(settings)

    positions = [{
        "symbol": "NVDA", "side": "long", "strategy": "momentum",
        "grade": "A", "quantity": 10, "entry_price": 100.0,
        "stop_price": 95.0, "target_price": 109.0, "current_price": 103.0,
        "unrealized_pnl": 30.0, "unrealized_pct": 3.0,
        "distance_to_stop_pct": 7.8, "distance_to_target_pct": 5.8,
        "r_progress": 0.6, "entry_time": "2026-07-07 10:00",
        "indicators": {"rsi": 61.0, "rsi_rising": True,
                       "rsi_overbought": False, "rsi_momentum_zone": True,
                       "macd_hist": 0.2, "macd_hist_expanding": True,
                       "macd_bullish": True, "ema20": 99.0, "ema200": 90.0,
                       "above_ema20": True, "above_ema200": True,
                       "volume_ratio": 2.1, "obv_confirming": True,
                       "fast_cloud_bullish": True,
                       "slow_cloud_bullish": True},
        "sentiment": None,
        "key_levels": {"support": [{"price": 97.0, "touches": 4}],
                       "resistance": [{"price": 106.0, "touches": 2}]},
    }]
    watchlist = [{
        "symbol": "AAPL", "status": "signal", "price": 200.0,
        "change_pct": 0.5,
        "signal": {"strategy": "swing", "grade": "B", "strength": 0.7,
                   "entry": 200.0, "stop": 195.0, "target": 215.0},
        "last_rejection": None, "indicators": None, "sentiment": None,
        "key_levels": {"support": [], "resistance": []},
    }]
    monkeypatch.setattr(ac, "build_position_facts", lambda s: positions)
    monkeypatch.setattr(ac, "build_watchlist_facts", lambda s: watchlist)
    monkeypatch.setattr(
        ac, "build_market_facts",
        lambda s: {"spy": {}, "qqq": {}, "vix": {}, "sectors": [],
                   "bias": {"label": "mixed", "note": ""}})

    payload = asyncio.run(engine.refresh(force=True))

    pos_card = payload["positions"][0]["card"]
    assert pos_card["kind"] == "position"
    assert pos_card["identity"]["symbol"] == "NVDA"
    assert pos_card["conditions"]["total"] == 5
    assert pos_card["stress_test"]["is_hypothetical"] is False

    wl_card = payload["watchlist"][0]["card"]
    assert wl_card["kind"] == "watchlist"
    assert wl_card["stress_test"]["is_hypothetical"] is True

    assert "rsi" in payload["term_help"]


# ------------------------------------------------------ auth-failure (401)


class _Fake401Client:
    """Async httpx client stub returning 401 for every POST."""

    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        import httpx

        _Fake401Client.calls += 1
        return httpx.Response(
            401,
            request=httpx.Request("POST", url),
            json={"error": {"message": "User not found.", "code": 401}},
        )


def test_401_sets_auth_failed_and_skips_further_calls(settings: Settings,
                                                      monkeypatch) -> None:
    """One 401 flags the key as rejected; the rest of the refresh skips the
    LLM (no budget burn), and everything falls back to template prose."""
    settings = Settings(DATA_DIR=settings.DATA_DIR,
                        OPENROUTER_API_KEY="bad-key")
    engine = _engine(settings)
    _Fake401Client.calls = 0
    monkeypatch.setattr(llm_router.httpx, "AsyncClient", _Fake401Client)

    positions = [{"symbol": "NVDA", "strategy": "momentum",
                  "entry_price": 100.0, "stop_price": 95.0,
                  "target_price": 109.0, "indicators": None,
                  "sentiment": None,
                  "key_levels": {"support": [], "resistance": []}}]
    watchlist = [{"symbol": "AAPL", "status": "idle", "price": 200.0,
                  "change_pct": 0.0, "readiness": 20, "signal": None,
                  "last_rejection": None, "indicators": None,
                  "sentiment": None,
                  "key_levels": {"support": [], "resistance": []}}]
    monkeypatch.setattr(ac, "build_position_facts", lambda s: positions)
    monkeypatch.setattr(ac, "build_watchlist_facts", lambda s: watchlist)
    monkeypatch.setattr(
        ac, "build_market_facts",
        lambda s: {"spy": {}, "qqq": {}, "vix": {}, "sectors": [],
                   "bias": {"label": "mixed", "note": ""}})

    payload = asyncio.run(engine.refresh(force=True))

    # Only the first call was attempted; the 401 short-circuited the rest.
    assert _Fake401Client.calls == 1
    assert payload["budget"]["used"] == 1
    assert engine._auth_failed is True
    assert payload["positions"][0]["source"] == "template"
    assert payload["watchlist"][0]["source"] == "template"
    assert payload["market"]["source"] == "template"


def test_401_degraded_state_is_plain_language(settings: Settings,
                                              monkeypatch) -> None:
    """The UI-facing state never leaks the raw provider error (spec:
    'Analysis unavailable — ...', not 'OpenRouter call failed: 401')."""
    settings = Settings(DATA_DIR=settings.DATA_DIR,
                        OPENROUTER_API_KEY="bad-key")
    engine = _engine(settings)
    monkeypatch.setattr(llm_router.httpx, "AsyncClient", _Fake401Client)
    monkeypatch.setattr(ac, "build_position_facts", lambda s: [])
    monkeypatch.setattr(ac, "build_watchlist_facts", lambda s: [])
    monkeypatch.setattr(
        ac, "build_market_facts",
        lambda s: {"spy": {}, "qqq": {}, "vix": {}, "sectors": [],
                   "bias": {"label": "mixed", "note": ""}})

    payload = asyncio.run(engine.refresh(force=True))

    assert payload["ai_state"] == "degraded"
    msg = payload["ai_state_message"]
    assert "API key" in msg
    assert "401" not in msg and "http" not in msg.lower()
    # The technical detail stays available for debugging via status().
    status = engine.status()
    assert status["auth_failed"] is True
    assert "401" in (status["last_error"] or "")


def test_ai_state_reports_missing_key(settings: Settings) -> None:
    engine = _engine(Settings(DATA_DIR=settings.DATA_DIR,
                              OPENROUTER_API_KEY=""))
    state = engine.ai_state()
    assert state["state"] == "degraded"
    assert "API key" in state["message"]


def test_ai_state_live_when_configured(settings: Settings) -> None:
    engine = _engine(Settings(DATA_DIR=settings.DATA_DIR,
                              OPENROUTER_API_KEY="k"))
    assert engine.ai_state()["state"] == "live"


# ------------------------------------------------------------------ router

@pytest.fixture
def client(settings: Settings, monkeypatch) -> TestClient:
    test_settings = Settings(DATA_DIR=settings.DATA_DIR,
                             DASHBOARD_AUTH_ENABLED=False)
    monkeypatch.setattr(dash, "get_settings", lambda: test_settings)
    ac.reset_engine()
    yield TestClient(dash.app, raise_server_exceptions=False)
    ac.reset_engine()


def _seed_fresh_payload(data_dir) -> dict:
    payload = {
        "generated_at": datetime.now(tz=ET).isoformat(),
        "positions": [], "watchlist": [],
        "market": {"spy": {"regime": "bull"}, "summary": "s",
                   "source": "template"},
        "budget": {"date": datetime.now(tz=ET).date().isoformat(),
                   "used": 1, "max": 150},
    }
    (data_dir / "ai_commentary.json").write_text(json.dumps(payload))
    return payload


def test_api_commentary_serves_cached_payload(client: TestClient,
                                              settings: Settings) -> None:
    _seed_fresh_payload(settings.DATA_DIR)
    resp = client.get("/api/ai/commentary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["market"]["spy"]["regime"] == "bull"
    assert body["stale"] is False
    assert body["budget"]["used"] == 1
    assert "next_refresh_at" in body
    assert "interval_minutes" in body


def test_api_market_overview(client: TestClient, settings: Settings) -> None:
    _seed_fresh_payload(settings.DATA_DIR)
    resp = client.get("/api/ai/market-overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["market"]["summary"] == "s"
    assert "positions" not in body


def test_api_refresh_429_when_budget_exhausted(client: TestClient,
                                               settings: Settings) -> None:
    payload = _seed_fresh_payload(settings.DATA_DIR)
    payload["budget"]["used"] = 10_000
    (settings.DATA_DIR / "ai_commentary.json").write_text(json.dumps(payload))
    ac.reset_engine()
    resp = client.post("/api/ai/refresh")
    assert resp.status_code == 429


def test_api_refresh_starts_background_refresh(client: TestClient,
                                               settings: Settings,
                                               monkeypatch) -> None:
    _seed_fresh_payload(settings.DATA_DIR)

    async def fake_refresh(self, force=False):
        return {}

    monkeypatch.setattr(CommentaryEngine, "refresh", fake_refresh)
    resp = client.post("/api/ai/refresh")
    assert resp.status_code == 200
    assert resp.json()["started"] is True


def test_api_status(client: TestClient, settings: Settings) -> None:
    _seed_fresh_payload(settings.DATA_DIR)
    resp = client.get("/api/ai/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "budget" in body and "model" in body and "market_open" in body


def test_ai_dashboard_page_renders(client: TestClient,
                                   settings: Settings) -> None:
    resp = client.get("/ai-dashboard")
    assert resp.status_code == 200
    assert "Analyst" in resp.text
    assert "/api/ai/commentary" in resp.text
    assert "Analysis, not advice" in resp.text


def test_ai_dashboard_implements_ux_spec_elements(client: TestClient,
                                                  settings: Settings) -> None:
    """The page ships the self-explanatory card UI: first-run tour, stress
    test with visible working + custom price input, per-card degraded
    styling — and none of the old verdict/score chrome."""
    resp = client.get("/ai-dashboard")
    html = resp.text
    # First-run tour (spec 6.2), re-triggerable from the "?" control.
    assert "ustb_analyst_tour" in html
    assert 'id="tourBox"' in html and 'id="tourBtn"' in html
    # Stress test (spec 5): scenarios, worked arithmetic, custom input.
    assert "Stress test" in html
    assert "customScenario" in html
    assert "Try your own price" in html
    # Conditions-met bar replaces any confidence % (spec 4.3 / 7).
    assert "conds-bar" in html
    # Per-card degraded treatment (spec 6.4): dashed border class.
    assert "card.degraded" in html
    # The old verdict/score chrome is gone.
    assert "sentimentChip" not in html
    assert "readiness" not in html
    assert "BULLISH" not in html


def test_main_dashboard_links_to_analyst(client: TestClient,
                                         settings: Settings) -> None:
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert 'href="/ai-dashboard"' in resp.text
