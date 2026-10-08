"""API Gateway & Orchestration — `services/api/` (FastAPI).

Responsibility (ARCHITECTURE §5.2): single public entry point. Contract
enforcement (Pydantic v2), authentication, rate limiting, and the Query
Orchestrator that fans out to retrieval, scoring and verification in the
correct order with timeouts.

Owns: route definitions, request/response schemas, orchestrator control flow,
per-request tracing id, latency budget enforcement, SSE stream lifecycle,
admin/ingestion trigger endpoints.

Must NOT own: vector math, rule evaluation logic, LLM prompting, browser
automation — these are delegated to their modules behind typed interfaces.

Failure behavior: downstream timeout -> return partial results with
verification_status = PENDING rather than a 5xx. Never block the eligibility
answer on TinyFish.

Endpoints (frozen):
    POST /api/v1/profile/qualify
    GET  /api/v1/schemes/{scheme_id}
    POST /api/v1/verify/{scheme_id}
    GET  /api/v1/checklist/{scheme_id}
    GET  /api/v1/verify/stream/{job_id}          (SSE)
    POST /api/v1/admin/ingest/refresh            (protected)

Latency budgets (p95): sync eligibility <= 1,200 ms · verified payload <= 6,000 ms.
"""
