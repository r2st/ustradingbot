"""Feedback API — unauthenticated endpoint for visitor feedback."""

from __future__ import annotations

import json
import time
from pathlib import Path

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

log = structlog.get_logger(__name__)

router = APIRouter(tags=["feedback"])

_FEEDBACK_DIR = Path(__file__).resolve().parent.parent / "data" / "feedback"


@router.post("/api/feedback")
async def submit_feedback(request: Request) -> JSONResponse:
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)
    if len(message) > 2000:
        return JSONResponse({"error": "message too long (max 2000 chars)"}, status_code=400)

    category = (body.get("category") or "general").strip()[:50]
    page = (body.get("page") or "").strip()[:200]

    entry = {
        "ts": time.time(),
        "category": category,
        "page": page,
        "message": message,
    }

    _FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{int(time.time() * 1000)}.json"
    (_FEEDBACK_DIR / filename).write_text(json.dumps(entry), encoding="utf-8")
    log.info("feedback_received", category=category, page=page)

    return JSONResponse({"ok": True}, status_code=201)
