"""P6f — trade journaling depth: setup/mistake tags, post-mortem, search."""

from __future__ import annotations

import json

from journal.notes import TradeNotesStore, _MAX_LIST, _MAX_TEXT


def _store(tmp_path):
    return TradeNotesStore(tmp_path)


def test_save_and_read_structured_fields(tmp_path):
    s = _store(tmp_path)
    s.set_note(
        "T1", note="clean break", setup_type="Breakout",
        mistake_tags=["Chased", "oversized"],
        what_worked="entered on retest", what_went_wrong="too big",
        lesson="size down on gaps", rating=4, tags=["win"],
    )
    n = s.get("T1").to_dict()
    assert n["setup_type"] == "Breakout"
    assert n["mistake_tags"] == ["chased", "oversized"]  # normalized + sorted
    assert n["what_worked"] == "entered on retest"
    assert n["lesson"] == "size down on gaps"
    assert n["rating"] == 4
    assert n["tags"] == ["win"]


def test_backward_compat_old_note_shape(tmp_path):
    # An old sidecar without the new fields must still load with defaults.
    old = {"T9": {"trade_id": "T9", "note": "legacy", "tags": ["old"],
                  "updated_at": "2026-01-01T00:00:00"}}
    (tmp_path / "trade_notes.json").write_text(json.dumps(old))
    s = _store(tmp_path)
    n = s.get("T9").to_dict()
    assert n["note"] == "legacy"
    assert n["setup_type"] == ""
    assert n["mistake_tags"] == []
    assert n["rating"] is None


def test_partial_update_leaves_other_fields(tmp_path):
    s = _store(tmp_path)
    s.set_note("T1", note="first", setup_type="pullback", rating=3)
    s.set_note("T1", lesson="wait for confirmation")  # only lesson
    n = s.get("T1").to_dict()
    assert n["note"] == "first"
    assert n["setup_type"] == "pullback"
    assert n["rating"] == 3
    assert n["lesson"] == "wait for confirmation"


def test_rating_clear_and_bounds(tmp_path):
    s = _store(tmp_path)
    s.set_note("T1", rating=5)
    assert s.get("T1").rating == 5
    s.set_note("T1", rating=None)  # explicit clear
    assert s.get("T1").rating is None
    s.set_note("T1", rating=99)    # out of range → cleared
    assert s.get("T1").rating is None


def test_caps_enforced(tmp_path):
    s = _store(tmp_path)
    s.set_note(
        "T1",
        note="x" * (_MAX_TEXT + 500),
        mistake_tags=[f"m{i}" for i in range(_MAX_LIST + 20)],
    )
    n = s.get("T1")
    assert len(n.note) == _MAX_TEXT
    assert len(n.mistake_tags) == _MAX_LIST


def test_tag_normalization_and_dedupe(tmp_path):
    s = _store(tmp_path)
    s.set_note("T1", mistake_tags=["FOMO!", "fomo", "  no-stop "])
    assert s.get("T1").mistake_tags == ["fomo", "no-stop"]


# ---------------------------------------------------------------------------
# search / facets
# ---------------------------------------------------------------------------


def _seed(tmp_path):
    s = _store(tmp_path)
    s.set_note("T1", note="broke out of base", setup_type="breakout",
               mistake_tags=["chased"], rating=2, tags=["loss"])
    s.set_note("T2", note="bought the dip", setup_type="pullback",
               mistake_tags=["early"], rating=5, tags=["win"])
    s.set_note("T3", note="reversal at support", setup_type="reversal",
               lesson="patience paid off", rating=4)
    return s


def test_search_by_query_substring(tmp_path):
    s = _seed(tmp_path)
    ids = {n.trade_id for n in s.search(query="dip")}
    assert ids == {"T2"}
    # query also matches a text field beyond note (lesson).
    assert {n.trade_id for n in s.search(query="patience")} == {"T3"}


def test_search_by_setup_type(tmp_path):
    s = _seed(tmp_path)
    assert {n.trade_id for n in s.search(setup_type="breakout")} == {"T1"}


def test_search_by_mistake_tag(tmp_path):
    s = _seed(tmp_path)
    assert {n.trade_id for n in s.search(mistake="chased")} == {"T1"}


def test_search_by_min_rating(tmp_path):
    s = _seed(tmp_path)
    assert {n.trade_id for n in s.search(min_rating=4)} == {"T2", "T3"}


def test_search_combined_and(tmp_path):
    s = _seed(tmp_path)
    # win tag AND rating>=5
    assert {n.trade_id for n in s.search(tag="win", min_rating=5)} == {"T2"}


def test_facets(tmp_path):
    s = _seed(tmp_path)
    f = s.facets()
    assert f["setup_types"] == ["breakout", "pullback", "reversal"]
    assert "chased" in f["mistake_tags"] and "early" in f["mistake_tags"]
    assert "win" in f["tags"] and "loss" in f["tags"]
