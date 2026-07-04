"""Tests for trade journal notes & tags (feature 3)."""

from __future__ import annotations

from datetime import datetime

from journal.notes import TradeNotesStore, get_notes_store, normalize_tag


def test_normalize_tag():
    assert normalize_tag(" #Mistake! ") == "mistake"
    assert normalize_tag("Gap-Up") == "gap-up"
    assert normalize_tag("") == ""


def test_set_and_get(tmp_data_dir):
    store = TradeNotesStore(tmp_data_dir)
    note = store.set_note("42", note="Chased the entry", tags=["mistake", "FOMO"])
    assert note.note == "Chased the entry"
    assert note.tags == ["fomo", "mistake"]
    assert store.get("42").note == "Chased the entry"


def test_partial_update_keeps_other_field(tmp_data_dir):
    store = TradeNotesStore(tmp_data_dir)
    store.set_note("1", note="first", tags=["a"])
    store.set_note("1", tags=["b"])  # note unchanged
    n = store.get("1")
    assert n.note == "first" and n.tags == ["b"]
    store.set_note("1", note="second")  # tags unchanged
    assert store.get("1").tags == ["b"]


def test_persistence(tmp_data_dir):
    TradeNotesStore(tmp_data_dir).set_note("7", note="held too long", tags=["patience"])
    reloaded = TradeNotesStore(tmp_data_dir)
    assert reloaded.get("7").note == "held too long"


def test_search_by_text_and_tag(tmp_data_dir):
    store = TradeNotesStore(tmp_data_dir)
    store.set_note("1", note="great gap up entry", tags=["winner"],
                   now=datetime(2026, 7, 1, 10, 0))
    store.set_note("2", note="stopped out fast", tags=["loser", "mistake"],
                   now=datetime(2026, 7, 2, 10, 0))
    store.set_note("3", note="clean gap fill", tags=["winner"],
                   now=datetime(2026, 7, 3, 10, 0))

    gap = store.search(query="gap")
    assert {n.trade_id for n in gap} == {"1", "3"}
    # Sorted most-recent first.
    assert gap[0].trade_id == "3"

    mistakes = store.search(tag="mistake")
    assert [n.trade_id for n in mistakes] == ["2"]

    combined = store.search(query="gap", tag="winner")
    assert {n.trade_id for n in combined} == {"1", "3"}


def test_all_tags(tmp_data_dir):
    store = TradeNotesStore(tmp_data_dir)
    store.set_note("1", tags=["a", "b"])
    store.set_note("2", tags=["b", "c"])
    assert store.all_tags() == ["a", "b", "c"]


def test_delete(tmp_data_dir):
    store = TradeNotesStore(tmp_data_dir)
    store.set_note("1", note="x")
    assert store.delete("1") is True
    assert store.get("1") is None
    assert store.delete("nope") is False


def test_get_notes_store_cached(tmp_data_dir):
    assert get_notes_store(tmp_data_dir) is get_notes_store(tmp_data_dir)
