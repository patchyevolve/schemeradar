# PM-Kisan live source and coverage review

**Run date:** 2026-10-11 10:40 UTC
**Candidate:** Pradhan Mantri Kisan Samman Nidhi (PM-Kisan)
**Catalog status:** `source_ready`
**Run boundary:** TinyFish discovery and fetch only. Stopped before LLM extraction; MongoDB and Qdrant were not connected or written.

## Search and fetch trace

Discovery query:

> PM Kisan eligibility benefits documents vikaspedia official scheme eligibility benefits application guidelines FAQ

TinyFish returned seven government-domain results:

1. [PM-Kisan Revised FAQ PDF](https://pmkisan.gov.in/Documents/RevisedFAQ.pdf) — search result centered on ineligible beneficiaries.
2. [PM-Kisan official portal](https://pmkisan.gov.in/) — homepage; TinyFish render failed transiently (`TinyFishUnavailable`). This is a fetch failure, not evidence that the scheme is unavailable.
3. [UMANG PM-Kisan service summary](https://web.umang.gov.in/landing/scheme/detail/pradhan-mantri-kisan-samman-nidhi_pm-kisan.html) — not selected within the bounded render set.
4. [PM-Kisan Operational Guidelines PDF](https://pmkisan.gov.in/Documents/RevisedPM-KISANOperationalGuidelines(English).pdf) — rendered successfully.
5. [PIB PM-Kisan PDF](https://static.pib.gov.in/WriteReadData/specificdocs/documents/2021/nov/doc2021112361.pdf) — rendered successfully; this is a 2021 source, not currentness evidence.
6. [Kisan Suvidha PM-Kisan page](https://kisansuvidha.gov.in/pm-kisan?back=1) — not selected within the bounded render set.
7. [Kerala PM-Kisan user manual PDF](https://keralaagriculture.gov.in/wp-content/uploads/2021/04/UserManual09092020.pdf) — not selected within the bounded render set.

The fetcher selected three pages for the evidence packet:

| Evidence role | Requested source | Fetch result | Visible text | Fetch time (UTC) | SHA-256 |
|---|---|---|---:|---|---|
| Guidelines | [PM-Kisan Operational Guidelines](https://pmkisan.gov.in/Documents/RevisedPM-KISANOperationalGuidelines(English).pdf) | TinyFish render succeeded; reported a raw-IP final URL | 28,122 chars | 2026-10-11 10:40:09 | `278204615e64aeec9495a94cae4b0bb2507bb920c1c4b336fe97369179512be8` |
| FAQ | [PM-Kisan Revised FAQ](https://pmkisan.gov.in/Documents/RevisedFAQ.pdf) | TinyFish render succeeded; reported a raw-IP final URL | 19,110 chars | 2026-10-11 10:40:09 | `8ad5c2f69161a56e9821db5a58b6c3d7f8158faa6fcc0518a8b687bd2f212fde` |
| Official PDF | [PIB PM-Kisan PDF](https://static.pib.gov.in/WriteReadData/specificdocs/documents/2021/nov/doc2021112361.pdf) | TinyFish render succeeded; reported a raw-IP final URL | 14,176 chars | 2026-10-11 10:40:41 | `c978a1ae5afef516648d9fdd9383d74a9b040dc3a9f182932137dd4569dcb79c` |

The evidence packet sent *if* parsing proceeded was capped at 3,500 characters across those three pages. No extraction request was made.

## Coverage review

| Coverage field | Result | Observed passage / issue |
|---|---|---|
| Scheme identity | Signal found | “Under the PM-KISAN scheme…” |
| Eligibility | Signal found | “eligible farmers”; exclusions and landholding conditions are also present |
| Benefit | **Needs numeric review** | Guidelines include “Rs.2000/- each”; the FAQ renderer returns “Rs.60001” where the PIB copy says “Rs.6000/-”. This may be a flattened footnote or rendering ambiguity, and must be checked against the PDF before normalizing an amount. |
| Required-document checklist | **Partial rule only** | “Aadhaar number shall be collected…” appears, but the packet does not show a complete application checklist. One document mention cannot establish an exhaustive list. |
| Application route | Signal found | “eligible farmers may apply with the village Patwaris, revenue officials…” This supports an offline route in the quoted FAQ text; it does not prove that the homepage is an application form. |
| Scheme continuation/current period | **Not found** | The selected substantive sources are guidelines/FAQ plus a 2021 PIB PDF. No current extension/continuation notice was found in this packet. The homepage render failed. |

### Gate outcome

The revised gate would stop before the LLM call with `evidence_incomplete`, because benefit formatting is ambiguous, document coverage is partial, and continuation evidence is absent. That is the appropriate outcome for this packet. It avoids treating a partial Aadhaar mention, an implementation portal mention, or a stale guideline as a complete and current scheme record.

## What this tells us about coverage review

The earlier lexical gate was not useful enough: it marked “Aadhaar” as document coverage and “portal” as an application route. This run exposed those false positives. The revised review records the matching passages, requires checklist language for document coverage, requires an application action tied to a channel, flags suspicious ungrouped currency values, and separates continuation evidence. On this PM-Kisan packet, it now catches the important gaps before extraction.

It is still a **screen**, not a semantic verifier. It cannot prove that a quotation entails a normalized rule, that every applicable document was listed, or that a government source is legally controlling. The user-facing system should not turn `signal_found` into “verified.” A human or stronger source-reconciliation step must resolve the current-period and amount issues before acceptance.

## Side effects

- LLM calls: **0**
- MongoDB writes: **0**
- Qdrant writes: **0**
- Tier 3/live application-status verification: **not run**

## Follow-up: targeted gap search (2026-10-11)

After the first review, a coverage-gap search was run for current continuation, benefit amount, and registration/document requirements. The first attempt hit a DNS resolution error. A later authorized coverage-only TinyFish run succeeded and fetched official material; its final packet and query trace are recorded in [PM_KISAN_COVERAGE_PACKET.json](PM_KISAN_COVERAGE_PACKET.json).

| Gap | New evidence found | What it establishes / does not establish |
|---|---|---|
| Current continuation | [PM India Cabinet release](https://www.pmindia.gov.in/en/news_updates/cabinet-approves-continuation-of-the-pm-kisan-scheme-from-2026-27-to-2030-31-with-a-financial-outlay-of-rs-3-15-lakh-crore/) says PM-KISAN continues from 2026–27 through 2030–31. | TinyFish fetched the release; the target-specific continuation passage is preserved in the final packet. |
| Benefit amount | The [current PM India release](https://www.pmindia.gov.in/en/news_updates/cabinet-approves-continuation-of-the-pm-kisan-scheme-from-2026-27-to-2030-31-with-a-financial-outlay-of-rs-3-15-lakh-crore/) and [PIB eligibility release](https://www.pib.gov.in/PressReleasePage.aspx?PRID=2146932) state ₹6,000 per year in three equal instalments. | Two separate official sources corroborate ₹6,000. The malformed `Rs.60001` line is retained as an audit warning and omitted from the parser packet. |
| Registration route and documents | The [official PM-Kisan FAQ PDF](https://pmkisan.gov.in/Documents/RevisedFAQ.pdf) says farmers may apply through village Patwaris/revenue officials and describes Aadhaar plus state land-record processes. The live PM-Kisan homepage and registration form did not reliably render in these runs. | Application route is supported. The source does **not** establish one exhaustive national upload checklist; retain document coverage as partial. |

### Updated gate interpretation

The final coverage-only run reports `ready_for_parse_with_document_review`: identity, eligibility, reconciled benefit, application route, and current continuation have target-specific evidence. The required-document field remains `partial_rule_only`. The parser now routes a successfully extracted candidate with partial/unknown document coverage to MongoDB human review and never Qdrant, rather than exposing a partial list as the complete citizen checklist. This packet does not establish an exhaustive national upload checklist.

The operational recovery is gap-directed retrieval: after coverage review, create one narrowly scoped query per missing or conflicting field, search official sources, fetch the best primary result, retain URL/date/hash/quoted passage, then rerun coverage. Searches and fetches run serially. The coverage-only CLI mode is `python -m tools.batch_ingest --limit 1 --coverage-only --coverage-report docs/PM_KISAN_COVERAGE_PACKET.json`.

### Final TinyFish run

- Outcome: `ready_for_parse_with_document_review`.
- Official source documents fetched: 4 (PM-Kisan Revised FAQ, PIB eligibility release, PIB benefit release, PM India continuation release).
- Source excerpt budget: 3,500 characters; target-field passages are retained before general context.
- Numeric conflict: malformed FAQ text `Rs.60001` was reconciled only after two independent official sources supported `Rs.6000`; the malformed line is excluded from the parser packet and retained as an audit warning.
- Continuation: matched to a passage that names PM-KISAN and states the 2026–27 through 2030–31 period. An unrelated Kisan Credit Card extension no longer qualifies.
- Documents: Aadhaar/data rule found, exhaustive checklist not found; this remains a review warning.
- Side effects: **0 LLM calls, 0 MongoDB writes, 0 Qdrant writes**.
