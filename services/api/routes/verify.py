"""Tier-3 verification stream — ``GET /api/v1/verify/stream/{scheme_id}``.

WORKFLOW §2.1: the browser agent emits one status string per algorithm step
and finishes on the verdict, so the frontend can paint a live progress badge
while the 4,000 ms Tier-3 budget runs.  Each step is flushed as an SSE frame:

    data: {"scheme_id": "...", "job_id": "...", "status": "navigating"}

    data: {"scheme_id": "...", "job_id": "...", "status": "INTAKE_OPEN",
           "done": true, "result": {...VerificationResult...}}

BUILD_ORDER §6.3 item 4.11 re-keys this endpoint to ``{job_id}`` once the
Step-4 queue exists; Step 3 streams synchronously per request because there is
no queue yet.

The agent is reached through :func:`make_agent`, a module-level factory the
tests replace, so importing this module never imports Playwright.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from services.api.config import get_settings
from services.tinyfish.agent import (
    TinyFishWebAgentClient,
    VerificationResult,
)

logger = logging.getLogger("schemeradar.api.verify")
router = APIRouter(prefix="/api/v1", tags=["verification"])

__all__ = ["router", "make_agent", "get_db", "portal_url_for", "sse"]

# Patchable indirection — tests swap this instead of monkeypatching Playwright.
make_agent: Any = TinyFishWebAgentClient

# Same shape as routes.admin: a module-level injectable for the pymongo
# Database the agent writes §2.3 steps 15-16 with.  ``None`` => lazy default.
get_db: Any = None


def _db_client() -> tuple[Any | None, Any]:
    """Return ``(client_to_close, database)`` for this stream."""
    if get_db is not None:
        return None, get_db()
    from pymongo import MongoClient

    s = get_settings()
    client = MongoClient(s.mongo_uri, serverSelectionTimeoutMS=1000)
    return client, client[s.mongo_db]


def portal_url_for(scheme_id: str) -> str:
    """Resolve ``Scheme.portal_url`` from MongoDB (sync, bounded by timeout)."""
    s = get_settings()
    client = MongoClient(s.mongo_uri, serverSelectionTimeoutMS=500)
    try:
        doc = client[s.mongo_db].schemes.find_one(
            {"scheme_id": scheme_id}, {"portal_url": 1, "_id": 0}
        )
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail=f"mongo unavailable: {type(exc).__name__}")
    finally:
        client.close()
    if not doc:
        raise HTTPException(status_code=404, detail=f"unknown scheme_id: {scheme_id}")
    url = str(doc.get("portal_url") or "").strip()
    if not url:
        raise HTTPException(status_code=422, detail=f"scheme has no portal_url: {scheme_id}")
    return url


def sse(payload: dict[str, Any]) -> bytes:
    """One SSE frame — ``data: {json}\\n\\n`` (WORKFLOW §2.1 stream shape)."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


@router.get(
    "/verify/stream/{scheme_id}",
    summary="Live Tier-3 portal verification (SSE)",
    description=(
        "Streams WORKFLOW §2.3 progress for `scheme_id`. The final frame "
        "carries `done: true` plus the strict `VerificationStatus` value "
        "(INTAKE_OPEN | INTAKE_CLOSED | UNREACHABLE | BLOCKED | ...)."
    ),
    responses={
        200: {"content": {"text/event-stream": {}}},
        404: {"description": "unknown scheme_id"},
        422: {"description": "scheme has no portal_url"},
    },
)
async def verify_stream(scheme_id: str) -> StreamingResponse:
    portal_url = portal_url_for(scheme_id)
    job_id = uuid.uuid4().hex

    async def gen() -> AsyncIterator[bytes]:
        client, db = _db_client()
        agent = make_agent(db=db)   # steps 15-16 need somewhere to land
        result: VerificationResult | None = None
        try:
            async for item in agent.events(scheme_id, portal_url, job_id):
                if isinstance(item, VerificationResult):
                    result = item
                    yield sse(
                        {
                            "scheme_id": scheme_id,
                            "job_id": job_id,
                            "status": item.verification_status.value,
                            "done": True,
                            "result": {
                                "verification_status": item.verification_status.value,
                                "extracted_deadline": (
                                    item.extracted_deadline.isoformat()
                                    if item.extracted_deadline else None
                                ),
                                "snapshot_url": item.snapshot_url,
                                "final_url": item.final_url,
                                "observed_signals": item.observed_signals,
                                "error_class": item.error_class,
                                "duration_ms": item.duration_ms,
                                "job_id": item.job_id,
                            },
                        }
                    )
                else:
                    yield sse(
                        {
                            "scheme_id": scheme_id,
                            "job_id": job_id,
                            "status": str(item),
                            "done": False,
                        }
                    )
        except Exception as exc:  # noqa: BLE001 — a 5xx here would break SSE
            logger.exception("verify stream %s failed", job_id)
            yield sse(
                {
                    "scheme_id": scheme_id,
                    "job_id": job_id,
                    "status": "UNREACHABLE",
                    "done": True,
                    "error": type(exc).__name__,
                    "result": None,
                }
            )
        finally:
            if client is not None:
                client.close()
            if result is None:
                logger.warning("verify stream %s ended without a result", job_id)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
