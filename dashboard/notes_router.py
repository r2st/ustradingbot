"""
Trade journal notes & tags API (feature 3).

CRUD + search over per-trade notes stored by :mod:`journal.notes`.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request

from config.settings import get_settings
from dashboard.auth import require_auth
from journal.notes import get_notes_store

router = APIRouter(prefix="/api/notes", tags=["notes"])


def _store():
    return get_notes_store(get_settings().DATA_DIR)


async def _body(request: Request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


@router.get("")
async def list_notes(q: str = "", tag: str = "", _user: str = Depends(require_auth)):
    """Search notes by substring *q* and/or exact *tag*."""
    store = _store()
    results = store.search(query=q, tag=tag)
    return {
        "notes": [n.to_dict() for n in results],
        "all_tags": store.all_tags(),
        "count": len(results),
    }


@router.get("/{trade_id}")
async def get_note(trade_id: str, _user: str = Depends(require_auth)):
    note = _store().get(trade_id)
    return note.to_dict() if note else {"trade_id": trade_id, "note": "", "tags": []}


@router.post("/{trade_id}")
async def set_note(trade_id: str, request: Request, _user: str = Depends(require_auth)):
    body = await _body(request)
    note = body.get("note")
    tags = body.get("tags")
    if tags is not None and not isinstance(tags, list):
        raise HTTPException(status_code=400, detail="tags must be a list.")
    try:
        saved = _store().set_note(trade_id, note=note, tags=tags)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return saved.to_dict()


@router.delete("/{trade_id}")
async def delete_note(trade_id: str, _user: str = Depends(require_auth)):
    return {"ok": _store().delete(trade_id)}
