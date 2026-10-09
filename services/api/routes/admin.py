"""Admin ingestion — ``POST /api/v1/admin/ingest/refresh``.

Triggers the Tier 1 → Tier 2 → Parse pipeline (ARCHITECTURE §7.1) for one
search string: ``Search → Fetch → L1–L5 ladder → accept | HRQ``.

Auth is **fail-closed** (BUILD_ORDER §6.3 item 4.13 "protected · Requires
ADMIN_SERVICE_TOKEN"):

* ``ADMIN_SERVICE_TOKEN`` unset/empty  -> 401 with an actionable detail. The
  endpoint is never open by default, not even in development.
* token present but wrong              -> 401.

The work runs as an ``asyncio`` task and the request returns **202** with a
``job_id``; progress is readable from the module-level ``JOBS`` map.  All four
collaborators (search, fetch, parse, db) are module-level injectables so tests
drive the whole flow with zero network, zero MongoDB and zero Playwright.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from services.api.config import get_settings
from services.tinyfish.parser import IngestOutcome, ingest_markdown

logger = logging.getLogger("schemeradar.api.admin")
router = APIRouter(prefix="/api/v1", tags=["admin"])

__all__ = [
    "router",
    "JOBS",
    "search_client_factory",
    "fetch_client_factory",
    "run_ingest",
    "get_db",
    "indexer_factory",
    "RefreshRequest",
    "RefreshAccepted",
    "require_admin",
    "refresh_pipeline",
]

BROWSER_CONCURRENCY_DEFAULT = 3   # ARCHITECTURE §7.4 — Tier 3 concurrency
MAX_RESULTS_CAP = 10

# --- injectables (tests replace these; production uses the real ones) ------
search_client_factory: Any = None      # -> TinyFishSearchClient()
fetch_client_factory: Any = None       # -> TinyFishFetchClient()
run_ingest: Any = ingest_markdown
get_db: Any = None                    # -> pymongo Database
indexer_factory: Any = None            # -> embed + Qdrant upsert

# job_id -> {"status": ..., "stage": ..., "hits": [...], "outcomes": [...]}
JOBS: dict[str, dict[str, Any]] = {}


def _search_client() -> Any:
    if search_client_factory is not None:
        return search_client_factory()
    from services.tinyfish.client import TinyFishSearchClient
    return TinyFishSearchClient()


def _fetch_client() -> Any:
    if fetch_client_factory is not None:
        return fetch_client_factory()
    from services.tinyfish.client import TinyFishFetchClient
    return TinyFishFetchClient()


def _db() -> Any:
    if get_db is not None:
        return get_db()
    from pymongo import MongoClient
    s = get_settings()
    client = MongoClient(s.mongo_uri, serverSelectionTimeoutMS=1000)
    return client[s.mongo_db]


def _indexer() -> Any:
    """Lazy embed → Qdrant upsert.  Imported on use so importing this module
    never loads ``sentence-transformers``."""
    if indexer_factory is not None:
        return indexer_factory()
    from services.api.config import get_settings as _gs
    from services.api.embedding import Embedder
    from services.api.storage import point_id, project_qdrant_payload
    from services.retrieval.text import scheme_text

    s = _gs()
    embedder = Embedder()

    def _index(model: Any) -> None:
        from qdrant_client import QdrantClient
        from qdrant_client.models import PointStruct

        doc = model.model_dump(mode="json")
        vector = embedder.encode([scheme_text(doc)])[0]
        client = QdrantClient(s.qdrant_url, timeout=30)
        client.upsert(
            collection_name=s.qdrant_collection,
            points=[
                PointStruct(
                    id=point_id(doc["scheme_id"]),
                    vector=vector,
                    payload=project_qdrant_payload(doc),
                )
            ],
        )

    return _index


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
async def require_admin(
    x_admin_token: str | None = Header(
        default=None,
        description="Equals ADMIN_SERVICE_TOKEN.",
    ),
    authorization: str | None = Header(default=None, description="Bearer <ADMIN_SERVICE_TOKEN>."),
) -> str:
    """Fail-closed admin gate (BUILD_ORDER §6.3 4.13)."""
    configured = (get_settings().admin_service_token or "").strip()
    if not configured:
        raise HTTPException(
            status_code=401,
            detail=(
                "ADMIN_SERVICE_TOKEN is not configured. Set it in .env "
                "(and .env.example) to enable ingestion."
            ),
        )
    presented = (x_admin_token or "").strip()
    if not presented and authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            presented = value.strip()
    if not presented or not hmac.compare_digest(presented, configured):
        raise HTTPException(status_code=401, detail="invalid admin token")
    return presented


# --------------------------------------------------------------------------
# Request / response
# --------------------------------------------------------------------------
class RefreshRequest(BaseModel):
    """Body for ``POST /api/v1/admin/ingest/refresh``."""

    query: str = Field(
        min_length=1,
        max_length=200,
        description="Tier-1 discovery search string (gov.in / nic.in only).",
    )
    max_results: int = Field(
        default=5,
        ge=1,
        le=MAX_RESULTS_CAP,
        description="Top-N URLs to Fetch+Parse (ARCHITECTURE §7.4 top-5).",
    )


class RefreshAccepted(BaseModel):
    status: Literal["accepted"] = "accepted"
    job_id: str
    query: str
    max_results: int


# --------------------------------------------------------------------------
# Pipeline — Search → Fetch → Parse
# --------------------------------------------------------------------------
async def refresh_pipeline(job_id: str, query: str, max_results: int) -> None:
    """Tier 1 → 2 → Parse for every discovered URL.  Never raises."""
    report = JOBS[job_id]
    try:
        # --- Tier 1: Search -------------------------------------------
        report["stage"] = "search"
        hits = await _search_client().discover([query], max_results=max_results)
        report["hits"] = [str(h.url) for h in hits]
        if not hits:
            report.update(status="completed", stage="done", note="no_gov_hits")
            return

        db = _db()
        indexer = _indexer()
        sem = asyncio.Semaphore(BROWSER_CONCURRENCY_DEFAULT)

        async def one(url: str) -> dict[str, Any]:
            async with sem:
                row: dict[str, Any] = {"url": url}
                try:
                    fetched = await _fetch_client().render(url)
                except Exception as exc:  # noqa: BLE001 — degradation, not 5xx
                    row.update(result="fetch_unreachable", error=type(exc).__name__)
                    return row
                row["http_status"] = int(fetched.get("http_status") or 0)
                try:
                    outcome: IngestOutcome = await run_ingest(
                        fetched.markdown,
                        fetch=fetched,
                        db=db,
                        indexer=indexer,
                    )
                except Exception as exc:  # noqa: BLE001
                    row.update(result="ingest_error", error=type(exc).__name__)
                    return row
                row.update(
                    result=outcome.result,
                    scheme_id=outcome.scheme_id,
                    index_state=outcome.index_state,
                    layer=outcome.layer,
                    repaired=outcome.repaired,
                    violations=outcome.violations[:20],
                )
                return row

        report["stage"] = "fetch+parse"
        rows = await asyncio.gather(*(one(u) for u in report["hits"]))
        report["outcomes"] = list(rows)
        report.update(status="completed", stage="done")
    except Exception as exc:  # noqa: BLE001 — background task must never escape
        logger.exception("refresh job %s failed", job_id)
        report.update(status="failed", stage=report.get("stage", "unknown"),
                      error=type(exc).__name__)


# --------------------------------------------------------------------------
# Route
# --------------------------------------------------------------------------
@router.post(
    "/admin/ingest/refresh",
    response_model=RefreshAccepted,
    status_code=202,
    summary="Kick off Search → Fetch → Parse (admin only)",
    description=(
        "Requires ADMIN_SERVICE_TOKEN via `X-Admin-Token` or "
        "`Authorization: Bearer`. Returns 202 immediately; poll the "
        "module-level job report for outcomes. Fail-closed: an unconfigured "
        "token is a 401, never an open endpoint."
    ),
    responses={401: {"description": "missing/unconfigured/invalid admin token"}},
)
async def ingest_refresh(
    body: RefreshRequest,
    _token: str = Depends(require_admin),
) -> RefreshAccepted:
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "status": "running",
        "stage": "queued",
        "query": body.query,
        "max_results": body.max_results,
        "hits": [],
        "outcomes": [],
    }
    # Fire-and-forget: the HTTP contract is the 202 + job_id (BUILD_ORDER 4.13).
    asyncio.create_task(refresh_pipeline(job_id, body.query, body.max_results))
    return RefreshAccepted(
        job_id=job_id, query=body.query, max_results=body.max_results
    )


@router.get(
    "/admin/ingest/jobs/{job_id}",
    summary="Read back an ingestion job report (admin only)",
    responses={404: {"description": "unknown job_id"}},
)
async def job_report(job_id: str, _token: str = Depends(require_admin)) -> dict[str, Any]:
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
    return dict(JOBS[job_id])
