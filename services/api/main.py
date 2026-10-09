"""SchemeRadar API Gateway entrypoint.

    uvicorn services.api.main:app --reload --port 8000

ARCHITECTURE §5.2 — single public entry point: contract enforcement
(Pydantic v2), authentication, rate limiting, and the Query Orchestrator.

Step 3 scaffolds the route layer (`services/api/routes/`): the dual-path
discovery/search route `POST /api/v1/profile/qualify` (ARCHITECTURE §5.2.1)
is mounted against *placeholder* retrieval and scoring functions, so
OpenAPI already publishes the contract the frontend binds to. The remaining
citizen/admin routes (§5.2 "Key endpoints"), the orchestrator fan-out, and
all scoring / TinyFish / Playwright behaviour land in BUILD_ORDER Step 4 —
no eligibility math runs here yet.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from services.api.config import get_settings
from services.api.routes import qualify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
)
logger = logging.getLogger("schemeradar.api")

APP_VERSION = "0.1.0-step3"
BUILD_ORDER_STEP = 3

settings = get_settings()

app = FastAPI(
    title="SchemeRadar API",
    version=APP_VERSION,
    description=(
        "AI-driven Indian civic-tech entitlement discovery and live link "
        "verification. Specification: docs/ARCHITECTURE.md §5.2."
    ),
    # OpenAPI served at /docs and /openapi.json.
)

# --- route registration ----------------------------------------------------
# One router per services/api/routes/<name>.py; each fixes its own /api/v1
# prefix so the mount order below carries no routing meaning.
app.include_router(qualify.router)


@app.get("/health", tags=["operability"])
async def health() -> JSONResponse:
    """Liveness + dependency probe (BUILD_ORDER Gate G1).

    Never raises: a datastore being down is *reported*, not converted into a
    5xx, mirroring ARCHITECTURE §8.2 — the system degrades and tells the truth
    about its own state.
    """
    deps: dict[str, Any] = {}

    # --- MongoDB ------------------------------------------------------------
    started = time.perf_counter()
    try:
        client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=500)
        client.admin.command("ping")
        client.close()
        deps["mongo"] = {"status": "up", "ms": round((time.perf_counter() - started) * 1000)}
    except PyMongoError as exc:
        deps["mongo"] = {"status": "down", "error": type(exc).__name__}

    # --- Qdrant -------------------------------------------------------------
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=0.5) as http:
            resp = await http.get(f"{settings.qdrant_url.rstrip('/')}/readyz")
            deps["qdrant"] = {
                "status": "up" if resp.status_code == 200 else "down",
                "ms": round((time.perf_counter() - started) * 1000),
            }
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        deps["qdrant"] = {"status": "down", "error": type(exc).__name__}

    # --- Object storage (MinIO) --------------------------------------------
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=0.5) as http:
            resp = await http.get(f"{settings.object_storage_endpoint.rstrip('/')}/minio/health/live")
            deps["object_storage"] = {
                "status": "up" if resp.status_code == 200 else "down",
                "ms": round((time.perf_counter() - started) * 1000),
            }
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        deps["object_storage"] = {"status": "down", "error": type(exc).__name__}

    healthy = all(d["status"] == "up" for d in deps.values())
    body = {
        "service": "schemeradar-api",
        "version": APP_VERSION,
        "build_order_step": BUILD_ORDER_STEP,
        "status": "ok" if healthy else "degraded",
        "dependencies": deps,
    }
    return JSONResponse(status_code=200, content=body)


@app.get("/api/v1/health", tags=["operability"], include_in_schema=False)
async def health_v1() -> JSONResponse:
    """Version-prefixed alias of /health."""
    return await health()


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {
        "service": "schemeradar-api",
        "docs": "/docs",
        "health": "/health",
        "specification": "docs/ARCHITECTURE.md",
    }
