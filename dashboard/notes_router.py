"""
Trade journal notes & tags API (feature 3).

CRUD + search over per-trade notes stored by :mod:`journal.notes`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from config.settings import get_settings
from dashboard.auth import require_auth
from dashboard.schemas import NoteRequest
from journal.notes import get_notes_store

router = APIRouter(prefix="/api/notes", tags=["Journal"])


def _store():
    return get_notes_store(get_settings().DATA_DIR)


@router.get("")
async def list_notes(
    q: str = "",
    tag: str = "",
    setup_type: str = "",
    mistake: str = "",
    min_rating: int | None = None,
    _user: str = Depends(require_auth),
):
    """Search/filter notes (P6f): substring *q*, exact *tag* / *mistake* /
    *setup_type*, and *min_rating*.  All optional, combined with AND."""
    store = _store()
    results = store.search(
        query=q, tag=tag, setup_type=setup_type, mistake=mistake,
        min_rating=min_rating,
    )
    return {
        "notes": [n.to_dict() for n in results],
        "all_tags": store.all_tags(),
        "count": len(results),
    }


# Declared before ``/{trade_id}`` so the literal paths win over the wildcard.
@router.get("/search")
async def search_notes(
    q: str = "",
    tag: str = "",
    setup_type: str = "",
    mistake: str = "",
    min_rating: int | None = None,
    _user: str = Depends(require_auth),
):
    """Alias of the list endpoint's filtered search (P6f)."""
    store = _store()
    results = store.search(
        query=q, tag=tag, setup_type=setup_type, mistake=mistake,
        min_rating=min_rating,
    )
    return {"notes": [n.to_dict() for n in results], "count": len(results)}


@router.get("/facets")
async def note_facets(_user: str = Depends(require_auth)):
    """Distinct setup types / mistake tags / tags to populate filter menus."""
    return _store().facets()


@router.get("/{trade_id}")
async def get_note(trade_id: str, _user: str = Depends(require_auth)):
    note = _store().get(trade_id)
    return note.to_dict() if note else {"trade_id": trade_id, "note": "", "tags": []}


@router.post("/{trade_id}")
async def set_note(
    trade_id: str, payload: NoteRequest, _user: str = Depends(require_auth)
):
    # exclude_unset keeps the rating sentinel meaningful: a field the client
    # never sent stays absent, so `body.get("rating", _UNSET)` still works.
    body = payload.model_dump(exclude_unset=True)
    tags = body.get("tags")
    if tags is not None and not isinstance(tags, list):
        raise HTTPException(status_code=400, detail="tags must be a list.")
    mistake_tags = body.get("mistake_tags")
    if mistake_tags is not None and not isinstance(mistake_tags, list):
        raise HTTPException(status_code=400, detail="mistake_tags must be a list.")
    # rating uses a sentinel so an explicit null clears it; absent leaves it.
    from journal.notes import _UNSET
    rating = body.get("rating", _UNSET)
    try:
        saved = _store().set_note(
            trade_id,
            note=body.get("note"),
            tags=tags,
            setup_type=body.get("setup_type"),
            mistake_tags=mistake_tags,
            what_worked=body.get("what_worked"),
            what_went_wrong=body.get("what_went_wrong"),
            lesson=body.get("lesson"),
            rating=rating,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return saved.to_dict()


@router.delete("/{trade_id}")
async def delete_note(trade_id: str, _user: str = Depends(require_auth)):
    return {"ok": _store().delete(trade_id)}
