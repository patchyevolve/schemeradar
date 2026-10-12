# TinyFish Steps 1–3 Readiness Review

**Review date:** 2026-10-10
**Scope:** Step 1 data contract, Step 2 persistence/API scaffolding, Step 3 TinyFish ingestion and Tier 3 verification. Step 4 scoring and Step 5 frontend are outside this review and were not implemented.

## Decision

The ingestion foundation is implemented, and its offline validation is substantially healthy. It is **not yet proven ready for teammate handoff as a complete live ingestion system**. The current evidence does not show reliable end-to-end ingestion from real government sites into MongoDB and Qdrant. Treat Step 3 as **implemented but live-gate incomplete**.

It can produce an eligible data record for a future scorer only when the selected official page contains the needed facts, extraction returns a schema-valid record, and source evidence passes validation. The current system deliberately routes unsupported or incomplete records to `pending_review`. This is the right safety behavior, but it means useful coverage is not guaranteed.

## What was checked

- `tools.validate_specs`: **4/4 phases passed**.
- `compileall` for TinyFish and the batch ingestion runner: **passed**.
- `tests/test_tinyfish.py` with `PYTHONPATH=.` and the API streaming/admin/OpenAPI tests excluded: **83 passed**.
- The full TinyFish test file reaches the streaming/API area and stalls. This review did not establish a clean full-suite result.
- Live smoke started with credentials configured, then stalled at Tier 1 search before returning hits. It was stopped without writes to MongoDB/Qdrant. Therefore Tier 1, Tier 2, live extraction, Tier 3, and MinIO were **not live-verified in this run**.
- The working-tree patch contains the ingestion evidence checks, source-vs-portal URL separation, forced `UNVERIFIED` Tier 2 status, revision records, DNS fail-closed handling, and batch retry/truncation changes. These are **uncommitted**.

## Step 1–3 behavior and limits

### Discovery and fetch

Tier 1 searches for government sources and limits accepted URLs to public HTTPS `*.gov.in` / `*.nic.in` hosts. Tier 2 renders selected pages and rejects empty/unhydrated SPA shells and PDFs in the batch candidate path. This reduces ingestion from unusable shells, but the runtime does not yet perform broad site exploration: the batch runner searches, considers a small candidate set, renders pages, and chooses a candidate. It does not reliably follow a site's menus, discover linked eligibility pages, fetch guideline PDFs and annexures together, or resolve where an application is actually accepted.

The current 3,500-character extraction excerpt is a budget compromise. It can cut off eligibility clauses, exceptions, document lists, state-specific notes, or the application link near the end of a long page. A correct response is then `pending_review`; a more concerning failure is a parseable but incomplete record if the omitted fact was not represented as a required field. Page selection and extraction need section-aware retrieval and multi-page source aggregation before coverage can be called complete.

### Extraction and data quality

The L1–L5 ladder, schema checks, confidence floor, source-span checks, repair limit, Mongo-before-index ordering, pending-review route, and revision capture provide a useful fail-closed base. This review patch additionally checks quoted spans against the fetched markdown and checks factual nested leaves for their own evidence paths. That rejects invented quotations and uncited values.

An exact quote check proves that the quote exists in the fetched page; it does **not** prove the quote entails the extracted value. Semantic support, unit/date normalization, contradictory clauses, scope (central vs state), amendment precedence, and stale facts still need explicit controls and review. Do not present every accepted row as fully authoritative just because it passed the schema ladder.

The application destination must remain distinct from the evidence page. The patch removes the previous behavior that copied `source_url` into `portal_url`; a missing application URL now causes review instead of presenting a press release as a citizen CTA. Tier 2 also forces `verification_status=UNVERIFIED` and `verified_at=null`; only the live verifier should establish current intake state.

### Persistence and retrieval handoff

Accepted records are embedded/upserted to Qdrant only after validation; MongoDB remains canonical and pending-review records should not be indexed. The patch adds a `scheme_revisions` record with content hash, parser version, provenance, and candidate JSON. This needs a production check against Mongo indexes/schema and duplicate/concurrent revision writes before relying on revisions for audit history.

Qdrant indexing is not the same as user retrieval. In `services/api/routes/qualify.py`, `retrieve_candidates()` and `score_candidates()` are still stubs returning empty lists. That belongs to the later scorer/team scope, but it means the current application cannot yet demonstrate that a citizen query retrieves these schemes. Handoff must explicitly state that Step 3 provides canonical records and vectors, not a working scorer/frontend path.

## Live corpus evidence in the handover

The existing handover note reports a historical three-seed run with **1 indexed and 2 pending review**, with Mongo moving 9→11 records and Qdrant 6→7 points. The two pending rows were attributed to Groq request-size errors and a malformed extracted application URL. This is useful operational evidence, but it is not a fresh run from this review and does not establish coverage of the 30-query corpus. The stated Groq rolling daily quota and long `Retry-After` wait also make a single uninterrupted corpus run unrealistic on the free tier.

## How to ingest government scheme data reliably with TinyFish

1. **Start from discovery, then map the official source set.** Search multiple natural-language variants and official domains for each scheme. Keep the original result URLs, titles, snippets, and fetch timestamps. Resolve redirects and canonical URLs. Do not treat one search result as the complete scheme.
2. **Build a source bundle per scheme.** Fetch the current scheme overview, operative guidelines/order, latest amendment/extension, FAQ, state/UT addenda where relevant, and the actual citizen application portal. Follow links from official pages with bounded depth and domain allowlists. Record which source controls each fact; the press release is usually discovery/context, not the governing rule.
3. **Render by document type.** Use browser rendering for JavaScript portals and HTML pages. Detect PDFs and download/extract them through a PDF text/OCR path; distinguish a text PDF from a scanned PDF and retain page numbers. Do not silently discard a relevant guideline because a browser renderer returned zero text.
4. **Extract into small, typed facts with evidence.** Store each factual value with exact quote, URL, page/section, fetch time, content hash, and source authority. Preserve original date/unit text alongside normalized values. Support repeated eligibility rules and document lists as arrays of independently evidenced facts.
5. **Reconcile before accepting.** Compare overview, guideline, amendment, FAQ, and state addendum. Apply explicit precedence and effective dates. If two authoritative sources disagree or the governing scope is unclear, create a review item with both citations. Never resolve conflicts by choosing the newest-looking page alone.
6. **Separate stable eligibility from live intake.** Benefits, income/land/age limits, category, residence, and documents belong in the durable scheme record with source/version dates. “Applications open,” deadlines, current budget, remaining quota, and portal availability are volatile. Verify them with the Tier 3 agent at query/display time and include observed time, URL, signals, and uncertainty. `UNREACHABLE` or `AMBIGUOUS` must not be converted into “open.”
7. **Index only reviewed accepted records.** Persist source bundle and revisions in MongoDB; embed the citizen-useful text and structured facts in Qdrant with scheme ID, jurisdiction, source freshness, and verification metadata. Reconcile one Qdrant point per indexed scheme and retain a rebuild path from MongoDB.
8. **Measure coverage, not just throughput.** For each seed, report discovered, fetched, parseable, accepted, pending review, and indexed counts; reason codes; source types; facts with citations; and retrieval sanity checks once the scorer exists. A `SKIP` is idempotency evidence, not proof that source data is current or correct.

## Handoff gates before calling Steps 1–3 complete

- Get a fresh live Tier 1→Tier 2→LLM→L1–L5 run to a MongoDB record and Qdrant point using an allowed test target; preserve redacted logs and reconcile counts.
- Get a live Tier 3 run and MinIO signed snapshot check; confirm current state is not claimed when the site is ambiguous or unreachable.
- Fix or explain the full TinyFish test-suite stall and report a clean full test result (or a documented subset plus the exact environmental blocker).
- Inspect revision uniqueness/concurrency behavior and migrate/index `scheme_revisions` as required.
- Run a representative corpus slice spanning HTML, SPA, text PDF, scanned PDF, redirects, and contradictory/amended sources. Keep pending review outcomes visible; do not optimize only for acceptance rate.
- Document API payloads and Mongo/Qdrant invariants for teammates. State clearly that qualify/scoring and frontend retrieval remain Step 4/5 work.

## Files in the working tree

The implementation edits are currently uncommitted in `services/tinyfish/agent.py`, `services/tinyfish/parser.py`, `tests/test_tinyfish.py`, and `tools/batch_ingest.py`; `docs/STEP_1_3_HANDOFF.md` is the operational handover. This review is an assessment, not a claim that all live gates passed.
