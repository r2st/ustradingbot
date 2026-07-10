"""Tests for F1 — the Learnings Engine: store, guard, and reflection."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pandas as pd
import pytest

from ai.reflection import ReflectionEngine, _parse_lesson
from analytics.learnings_guard import ALLOW, DEMOTE, REJECT, LearningsGuard
from config.settings import EASTERN, Settings
from journal.learnings import (
    AVOID,
    OBSERVE,
    PREFER,
    REQUIRE_CONFIRM,
    Learning,
    LearningStore,
    default_expiry,
)
from signals.signal_types import Grade, Signal


def _sig(grade: Grade = Grade.B, rsi: float = 70.0, vol: float = 1.5,
         strategy: str = "momentum", direction: str = "long") -> Signal:
    return Signal(
        symbol="TSLA", strategy=strategy, direction=direction,
        entry_price=100.0, stop_price=95.0, target_price=115.0,
        signal_strength=0.70, grade=grade, rsi_value=rsi, volume_ratio=vol,
    )


def _learning(**kw) -> Learning:
    now = datetime.now(tz=EASTERN)
    base = dict(
        id="lrn_test_001", strategy="momentum", direction="long",
        created_at=now.isoformat(),
        expires_at=default_expiry(now, 90),
        lesson_text="Test lesson.", action=OBSERVE,
        conditions={}, confidence=0.9, support_count=5,
    )
    base.update(kw)
    return Learning(**base)


# ----------------------------------------------------------------- store

def test_store_roundtrip(tmp_data_dir) -> None:
    store = LearningStore(tmp_data_dir)
    store.append(_learning(id="lrn_1", action=AVOID))
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].id == "lrn_1"
    assert loaded[0].action == AVOID


def test_store_skips_malformed_lines(tmp_data_dir) -> None:
    store = LearningStore(tmp_data_dir)
    store.append(_learning(id="lrn_ok"))
    with open(store.path, "a", encoding="utf-8") as f:
        f.write("this is not json\n")
        f.write("\n")
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].id == "lrn_ok"


def test_store_filters_expired(tmp_data_dir) -> None:
    store = LearningStore(tmp_data_dir)
    past = (datetime.now(tz=EASTERN) - timedelta(days=1)).isoformat()
    store.append(_learning(id="lrn_expired", expires_at=past))
    store.append(_learning(id="lrn_live"))
    assert [l.id for l in store.load()] == ["lrn_live"]
    assert len(store.load(include_expired=True)) == 2


def test_store_prune_expired(tmp_data_dir) -> None:
    store = LearningStore(tmp_data_dir)
    past = (datetime.now(tz=EASTERN) - timedelta(days=1)).isoformat()
    store.append(_learning(id="lrn_expired", expires_at=past))
    store.append(_learning(id="lrn_live"))
    removed = store.prune_expired()
    assert removed == 1
    assert len(store.load(include_expired=True)) == 1


def test_store_next_id_increments(tmp_data_dir) -> None:
    store = LearningStore(tmp_data_dir)
    now = datetime(2026, 7, 10, tzinfo=EASTERN)
    id1 = store.next_id(now)
    store.append(_learning(id=id1))
    id2 = store.next_id(now)
    assert id1 == "lrn_20260710_001"
    assert id2 == "lrn_20260710_002"


# ----------------------------------------------------------------- guard

def test_guard_no_learnings_allows(settings: Settings, tmp_data_dir) -> None:
    guard = LearningsGuard(settings, LearningStore(tmp_data_dir))
    assert guard.evaluate(_sig()).action == ALLOW


def test_guard_avoid_rejects_when_conditions_match(settings: Settings, tmp_data_dir) -> None:
    store = LearningStore(tmp_data_dir)
    store.append(_learning(action=AVOID, conditions={"rsi_above": 65}))
    guard = LearningsGuard(settings, store)
    v = guard.evaluate(_sig(rsi=70.0))
    assert v.action == REJECT
    assert v.rejects


def test_guard_avoid_skipped_when_conditions_dont_match(settings: Settings, tmp_data_dir) -> None:
    store = LearningStore(tmp_data_dir)
    store.append(_learning(action=AVOID, conditions={"rsi_above": 75}))
    guard = LearningsGuard(settings, store)
    # RSI 70 is not above 75 → lesson doesn't apply → allow.
    assert guard.evaluate(_sig(rsi=70.0)).action == ALLOW


def test_guard_require_confirm_demotes_grade_b(settings: Settings, tmp_data_dir) -> None:
    store = LearningStore(tmp_data_dir)
    store.append(_learning(action=REQUIRE_CONFIRM))
    guard = LearningsGuard(settings, store)
    assert guard.evaluate(_sig(grade=Grade.B)).action == DEMOTE


def test_guard_require_confirm_allows_grade_a(settings: Settings, tmp_data_dir) -> None:
    store = LearningStore(tmp_data_dir)
    store.append(_learning(action=REQUIRE_CONFIRM))
    guard = LearningsGuard(settings, store)
    assert guard.evaluate(_sig(grade=Grade.A)).action == ALLOW


def test_guard_prefer_annotates_only(settings: Settings, tmp_data_dir) -> None:
    store = LearningStore(tmp_data_dir)
    store.append(_learning(action=PREFER, lesson_text="Great setup."))
    guard = LearningsGuard(settings, store)
    v = guard.evaluate(_sig())
    assert v.action == ALLOW
    assert v.prefer_notes == ["Great setup."]


def test_guard_observe_is_non_binding(settings: Settings, tmp_data_dir) -> None:
    store = LearningStore(tmp_data_dir)
    store.append(_learning(action=OBSERVE))
    guard = LearningsGuard(settings, store)
    assert guard.evaluate(_sig()).action == ALLOW


def test_guard_direction_and_strategy_scoped(settings: Settings, tmp_data_dir) -> None:
    store = LearningStore(tmp_data_dir)
    store.append(_learning(action=AVOID, strategy="swing"))
    store.append(_learning(action=AVOID, direction="short"))
    guard = LearningsGuard(settings, store)
    # A long momentum signal matches neither lesson.
    assert guard.evaluate(_sig(strategy="momentum", direction="long")).action == ALLOW


def test_guard_disabled_allows(settings: Settings, tmp_data_dir) -> None:
    store = LearningStore(tmp_data_dir)
    store.append(_learning(action=AVOID, conditions={"rsi_above": 65}))
    disabled = settings.model_copy(update={"LEARNINGS_ENABLED": False})
    guard = LearningsGuard(disabled, store)
    assert guard.evaluate(_sig(rsi=70.0)).action == ALLOW


# --------------------------------------------------------- lesson parsing

def test_parse_lesson_strict_json() -> None:
    out = _parse_lesson(
        '{"lesson_text": "Skip weak volume.", "pattern_tags": ["a", "b"], '
        '"action": "avoid", "conditions": {"rsi_above": 65, "volume_ratio_below": 2.0}, '
        '"confidence": 0.8}'
    )
    assert out is not None
    assert out["action"] == "avoid"
    assert out["conditions"] == {"rsi_above": 65.0, "volume_ratio_below": 2.0}
    assert out["confidence"] == 0.8


def test_parse_lesson_wrapped_in_prose() -> None:
    out = _parse_lesson('Here you go:\n```json\n{"lesson_text": "x", "action": "prefer", "confidence": 0.5}\n```')
    assert out is not None
    assert out["action"] == "prefer"


def test_parse_lesson_drops_bad_conditions_and_clamps_confidence() -> None:
    out = _parse_lesson(
        '{"lesson_text": "x", "conditions": {"rsi_above": "high", "grade_max": "Z"}, "confidence": 5}'
    )
    assert out is not None
    assert out["conditions"] == {}         # non-numeric / invalid dropped
    assert out["confidence"] == 1.0        # clamped to [0, 1]


def test_parse_lesson_rejects_empty_and_textless() -> None:
    assert _parse_lesson("") is None
    assert _parse_lesson("no json here") is None
    assert _parse_lesson('{"action": "avoid"}') is None  # no lesson_text


# --------------------------------------------------------- reflection engine

def _closed_trade(pnl: float = -142.30) -> dict:
    return {
        "trade_id": "7", "symbol": "TSLA", "strategy": "momentum",
        "direction": "long", "grade": "B", "rsi_value": 68.2,
        "volume_ratio": 1.8, "macd_histogram": 0.45, "ema_score": 0.72,
        "entry_fill_price": 100.0, "stop_price": 95.0, "target_price": 115.0,
        "exit_reason": "STOP_HIT", "pnl_net": pnl, "r_multiple": -0.85,
        "hold_duration_hours": 72.0,
    }


def _canned_chat(response: str):
    async def _chat(settings, **kwargs):
        return response, {"prompt_tokens": 10, "completion_tokens": 20}
    return _chat


def test_reflect_stores_binding_lesson_with_support(settings: Settings, tmp_data_dir, monkeypatch) -> None:
    store = LearningStore(tmp_data_dir)
    engine = ReflectionEngine(settings, store)
    monkeypatch.setattr(
        "ai.reflection.chat",
        _canned_chat('{"lesson_text": "Avoid this.", "action": "avoid", '
                     '"conditions": {"rsi_above": 65}, "confidence": 0.85}'),
    )
    # Provide 5 similar historical trades so the support gate is satisfied.
    similar = pd.DataFrame([{
        "strategy": "momentum", "direction": "long", "grade": "B",
        "rsi_value": 68.0, "volume_ratio": 1.8, "pnl_net": -50.0,
        "r_multiple": -1.0,
        "exit_time": (datetime.now(tz=EASTERN) - timedelta(days=3)).isoformat(),
        "hold_duration_hours": 24.0,
    } for _ in range(5)])

    lrn = asyncio.run(engine.reflect(_closed_trade(), all_trades=similar))
    assert lrn is not None
    assert lrn.action == AVOID          # binding: enough support + confidence
    assert lrn.support_count == 5
    assert store.count() == 1


def test_reflect_downgrades_to_observe_without_support(settings: Settings, tmp_data_dir, monkeypatch) -> None:
    store = LearningStore(tmp_data_dir)
    engine = ReflectionEngine(settings, store)
    monkeypatch.setattr(
        "ai.reflection.chat",
        _canned_chat('{"lesson_text": "Avoid this.", "action": "avoid", "confidence": 0.85}'),
    )
    # No similar history → support 0 < 3 → downgraded to non-binding observe.
    lrn = asyncio.run(engine.reflect(_closed_trade(), all_trades=pd.DataFrame()))
    assert lrn is not None
    assert lrn.action == OBSERVE
    assert lrn.support_count == 0


def test_reflect_downgrades_low_confidence(settings: Settings, tmp_data_dir, monkeypatch) -> None:
    store = LearningStore(tmp_data_dir)
    engine = ReflectionEngine(settings, store)
    monkeypatch.setattr(
        "ai.reflection.chat",
        _canned_chat('{"lesson_text": "Maybe avoid.", "action": "avoid", '
                     '"conditions": {"rsi_above": 65}, "confidence": 0.2}'),
    )
    similar = pd.DataFrame([{
        "strategy": "momentum", "direction": "long", "grade": "B",
        "rsi_value": 68.0, "volume_ratio": 1.8, "pnl_net": -50.0, "r_multiple": -1.0,
        "exit_time": (datetime.now(tz=EASTERN) - timedelta(days=3)).isoformat(),
        "hold_duration_hours": 24.0,
    } for _ in range(5)])
    lrn = asyncio.run(engine.reflect(_closed_trade(), all_trades=similar))
    assert lrn.action == OBSERVE        # confidence 0.2 < 0.6 → non-binding


def test_reflect_fail_open_on_bad_output(settings: Settings, tmp_data_dir, monkeypatch) -> None:
    store = LearningStore(tmp_data_dir)
    engine = ReflectionEngine(settings, store)
    monkeypatch.setattr("ai.reflection.chat", _canned_chat("not json at all"))
    lrn = asyncio.run(engine.reflect(_closed_trade(), all_trades=pd.DataFrame()))
    assert lrn is None
    assert store.count() == 0


def test_reflect_disabled_returns_none(settings: Settings, tmp_data_dir, monkeypatch) -> None:
    disabled = settings.model_copy(update={"LEARNINGS_ENABLED": False})
    store = LearningStore(tmp_data_dir)
    engine = ReflectionEngine(disabled, store)
    called = {"n": 0}

    async def _boom(*a, **k):
        called["n"] += 1
        return "", {}

    monkeypatch.setattr("ai.reflection.chat", _boom)
    lrn = asyncio.run(engine.reflect(_closed_trade()))
    assert lrn is None
    assert called["n"] == 0             # short-circuits before any AI call
