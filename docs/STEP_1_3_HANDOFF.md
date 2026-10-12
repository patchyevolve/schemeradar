# SchemeRadar Steps 1–3 Handoff

Status: implementation handoff for Steps 1–3 only. Step 4 scoring and Step 5 client work remain teammate scope.

## Frozen scope

- Step 1: domain models and data contracts.
- Step 2: MongoDB/Qdrant storage and FastAPI scaffolding.
- Step 3: TinyFish Tier 1 search, Tier 2 render, L1–L5 ingestion validation, Tier 3 Playwright verification, and MinIO evidence.
- Do not add scoring math under `services/scoring/` or change qualify-route scoring stubs.

## Ingestion and safety invariants

- Tier 1 results are limited to public HTTPS `*.gov.in` / `*.nic.in` URLs.
- Tier 2 candidates ending in `.pdf` or rendering fewer than 500 characters are skipped. Oversized markdown is truncated before extraction; provenance is checked against the exact bytes sent to the LLM.
- L1–L5 is fail-closed. Only validated candidates are committed as indexed and embedded into Qdrant. Failed candidates are written to MongoDB as `pending_review` and never embedded.
- Batch ingestion bypasses Tier 3 by design. Tier 3 remains on-demand through `GET /api/v1/verify/stream/{scheme_id}`.
- Tier 3 performs SSRF/DNS preflight, enforces the 6,000 ms budget, records an auditable result, and uses short-lived signed snapshot URLs. DNS resolver failures now produce `UNREACHABLE` / `dns_failure` instead of escaping the stream.
- Admin ingestion remains fail-closed when `ADMIN_SERVICE_TOKEN` is unset or incorrect.

## Current live corpus snapshot

From `/tmp/opencode/verify.log`, the latest three-seed run completed with:

| Result | Count |
| --- | ---: |
| Indexed | 1 |
| Pending review | 2 |

MongoDB moved from 9 to 11 records (6 to 7 indexed; 3 to 4 pending review). Qdrant moved from 6 to 7 points, preserving one point per indexed scheme. The accepted record was PM Garib Kalyan Anna Yojana. Ayushman was routed to review after Groq HTTP 413 TPM errors; Ujjwala was routed to review after the extracted `portal_url` failed schema validation. These outcomes preserve the fail-closed contract; they are not evidence that all 30 seeds have been ingested.

## Batch runner behavior

`tools/batch_ingest.py` reads `data/seeds.json`, skips already indexed matches, chooses hydrated scheme pages over empty SPA/PDF results, sends a bounded source excerpt, and reports before/after MongoDB and Qdrant counts. Groq wait hints accept Go-style durations such as `1h4m32.448s`; the runner honors up to a 24-hour provider hint with a bounded retry count. Current source excerpt cap is 3,500 characters to leave room for the fixed extraction prompt under the 8k TPM budget. The free-tier daily budget still constrains throughput; continue in small batches or configure a paid/higher-limit provider.

Run a batch explicitly; this writes to MongoDB and Qdrant unless `--dry-run` is passed:

```bash
.venv/bin/python -m tools.batch_ingest --limit 2
.venv/bin/python -m tools.batch_ingest --dry-run --limit 1
```

## Verification runbook

```bash
.venv/bin/python -m tools.validate_specs
.venv/bin/python -m pytest tests/ -q
.venv/bin/python tools/smoke_e2e_live.py
```

`validate_specs` currently passes 4/4 phases and compile checks pass. The last local pytest attempt printed multiple failures and did not complete cleanly in this environment, so the prior “161 passed” result must be rerun by the receiving team. The live smoke requires TinyFish, LLM, MongoDB, Qdrant, Playwright/browser, and MinIO credentials/services; run it before declaring the end-to-end live gate complete. Use ports 8123/8124 for local API checks; do not bind 8000 or 8077.

## Handoff checklist for receiving team

1. Confirm `.venv` dependencies and `.env` values without printing secrets.
2. Run the full tests and investigate failures before merging.
3. Run `tools/smoke_e2e_live.py` and save its redacted output as the live Tier 1–3 + MinIO evidence.
4. Resume the 30-query seed corpus in small live batches, recording indexed/review counts and keeping Qdrant count equal to indexed-scheme count.
5. Review `pending_review` documents, especially provider throttling and malformed extraction fields; do not force-index them.
6. Keep Steps 4 and 5 in the assigned teammate branch/scope.

## Current working tree changes

- `tools/batch_ingest.py`: correct Go-duration parsing is already in the working tree; provider waits are honored up to 24 hours and source markdown is capped at 3,500 characters.
- `services/tinyfish/agent.py`: transient DNS preflight errors fail closed as `UNREACHABLE`.
- `tests/test_tinyfish.py`: offline regression case for the Tier 3 DNS behavior.

These changes are uncommitted and are ready for review. The live smoke and a clean full test run remain release gates.
