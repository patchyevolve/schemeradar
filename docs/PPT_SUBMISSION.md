# SchemeRadar — AZINHACK '26 Pitch Deck & Screening Script

> **Document Status:** Phase 4 of 5 — *Approved Baseline*
> **Version:** 1.0.0
> **Event:** AZINHACK '26 · 21–22 October 2026 · USAR, GGSIPU East Delhi Campus · **Day 2 Grand Finale**
> **Scope:** Exact content + speaker notes for the 9 mandatory screening slides, and a timed 3-minute oral pitch script.
> **Precedent:** `docs/ARCHITECTURE.md` (Phase 1), `docs/DATA_SPEC.md` (Phase 2), `docs/WORKFLOW_AND_TESTS.md` (Phase 3). Every number spoken or shown is traceable to those documents — see [§10 Consistency Appendix](#10-consistency-appendix--every-number-its-source).
> **Constraint:** No application source code. This is presentation specification.

---

## Table of Contents

1. [Rubric Mapping & Deck Rules](#1-rubric-mapping--deck-rules)
2. [Slide 1 — Problem Statement](#slide-1--problem-statement)
3. [Slide 2 — Proposed Solution](#slide-2--proposed-solution)
4. [Slide 3 — Target Users & Real-World Use Cases](#slide-3--target-users--real-world-use-cases)
5. [Slide 4 — Role of Machine Learning & AI](#slide-4--role-of-machine-learning--ai)
6. [Slide 5 — Proposed Technical Approach](#slide-5--proposed-technical-approach)
7. [Slide 6 — Proposed TinyFish Integration](#slide-6--proposed-tinyfish-integration)
8. [Slide 7 — Expected Impact](#slide-7--expected-impact)
9. [Slide 8 — Feasibility & Cost-Scalability](#slide-8--feasibility--cost-scalability)
10. [Slide 9 — Future Scope](#slide-9--future-scope)
11. [Timed 3-Minute Oral Pitch Script](#timed-3-minute-oral-pitch-script)
12. [Consistency Appendix](#10-consistency-appendix--every-number-its-source)

---

## 1. Rubric Mapping & Deck Rules

### 1.1 Mandatory screening rubric → slide → evidence

| # | Mandatory slide | Rubric question it answers | Primary evidence document |
|---|---|---|---|
| 1 | **Problem Statement** | Is the problem real, large, and specific to India? | Concept spec + Phase 1 §1 |
| 2 | **Proposed Solution** | Does the solution directly dissolve the stated problem? | Phase 1 §1.3, §2 |
| 3 | **Target Users & Real-World Use Cases** | Are users concrete, and are use cases grounded? | Phase 2 §4, §5; Phase 3 §3.1 |
| 4 | **Role of Machine Learning & AI** | Is AI load-bearing, not decorative? | Phase 1 §6 |
| 5 | **Proposed Technical Approach** | Is the architecture coherent and buildable? | Phase 1 §2, §5 |
| 6 | **Proposed TinyFish Integration** | Are all three TinyFish surfaces used with clear intent? | Phase 1 §7 |
| 7 | **Expected Impact** | Is impact quantifiable and honest? | Phase 1 §1.2; Phase 3 §1 |
| 8 | **Feasibility & Cost-Scalability** | Will it work under real load and real money? | Phase 1 §8; Phase 2 §7.7 |
| 9 | **Future Scope** | Is there a credible 12-month horizon? | Phase 2 §2 (schema readiness); Phase 3 §1.5 |

### 1.2 Deck rules

- **One idea per slide.** Title = the claim; bullets = the proof.
- **Never improvise a number.** If a figure is not in the Consistency Appendix (§10), do not say it.
- **Projections must be labelled** on-slide as `target` or `design capacity` — never stated as measured results.
- **Terminology lock:** `Neuro-Symbolic Scoring Engine` (shortened to `Neuro-Symbolic Engine` on stage only), `Hybrid Retrieval (Dense + BM25)`, `RRF k = 60`, `S_det`, `S_sem`, `P_docs`, `Bridge the Gap Checklist`, `verification_status`, `TinyFish Search / Fetch / Web Agent`, `fail-closed Human Review Queue`. These names are frozen across Phases 1–3 and must not be paraphrased on stage.
- **Suggested pacing for a 10-slide deck (9 content + title):** 12–15 seconds per slide; the 3-minute script in §11 is the authoritative timing.

---

## Slide 1 — Problem Statement

**Title:** *₹1.4 Lakh Crore Is Being Allocated. 65% Never Reaches the Person It Was Written For.*

### On-slide content

- **₹1.4 Lakh Crore+** allocated annually to Indian welfare schemes — scholarships, fee reimbursements, maternal healthcare, farmer machinery subsidies, skill grants.
- **65%+ of eligible citizens never access their entitlement.** Not an awareness gap alone — a *retrieval, comprehension, and verification* gap.
- **Three structural walls:**
  1. **Portal fragmentation** — hundreds of unlinked state portals. Standard crawlers fail on **legacy ASP.NET postbacks**, dynamic JS tables, session-locked forms (Delhi e-District, Haryana Antyodaya-SARAL, NSP, state labour welfare boards).
  2. **Legalese gating** — eligibility lives in **40–60 page scanned gazette PDFs**. "Creamy Layer exclusion", "SDM-attested family ceiling", "domicile tenure" are unreadable to a beneficiary.
  3. **Ghost & expired links** — aggregator blogs serve **dead pages, 2022/23 deadlines, and ad redirects** as if they were live schemes.

*Visual: a funnel leaking rupee symbols through three labelled cracks.*

### Speaker notes

- Open with scale, not technology — the jury must feel the money before they hear the stack.
- "Sixty-five percent" is not a literacy statistic; it is the compound failure of three *engineering* problems. Frame it that way: this is a data-retrieval problem wearing a social-policy costume.
- Name the portals out loud. Judges in Delhi recognise e-District and Antyodaya-SARAL instantly — specificity buys credibility.
- Emphasise the word **verified**: the third wall is the one nobody in this room has solved, and it is the one that wastes the most citizen time.
- Land the sentence: *"The schemes exist. The citizen's path to them does not."*
- **Do not** claim we measured the 65% ourselves — attribute it to the widely cited figure in our problem framing.

---

## Slide 2 — Proposed Solution

**Title:** *SchemeRadar — Agentic Entitlement Discovery & Live Verification*

### On-slide content

**One sentence:** *Enter a 60-second profile → get a ranked, live-verified, gap-analyzed list of schemes you actually qualify for.*

**The four-stage pipeline:**

| Stage | What it does |
|---|---|
| **1 · Hybrid Retrieval** | Dense embeddings (semantic intent) + BM25 (hard legal tokens) fused with **RRF (k = 60)** → top-30 candidates |
| **2 · Neuro-Symbolic Eligibility Engine** | Deterministic hard gates ($S_{det} \in \{0,1\}$) + LLM Semantic Auditor ($\lambda_{llm}$) + Document Friction Penalty ($P_{docs}$) |
| **3 · Autonomous Live Verification** | TinyFish Web Agent drives a cloud browser to prove the intake window is **open right now** |
| **4 · Citizen Action Dashboard** | Exact ₹ value · confidence % · **Bridge the Gap Checklist** · verified portal link |

$$Score = W_D \cdot S_{det} + W_S \cdot S_{sem} - P_{docs} \qquad (W_D = 0.60,\ W_S = 0.40)$$

- **Honest by construction:** borderline cases are routed to `NEAR_MISS`, never inflated.
- **Fresh by construction:** no link shown without verification in the **last 60 minutes**.

*Visual: the four boxes left-to-right, with the formula beneath.*

### Speaker notes

- Say the one-sentence value proposition verbatim — it is the deck's spine.
- Stress the word **agentic**: this is not a search box over a spreadsheet. Something *goes and looks*.
- The formula is on the slide for a reason — it tells the jury we did not outsource judgement to an LLM. Explain it in one line: *"Rules decide if you're eligible; retrieval decides how relevant; the penalty decides how ready you are with paperwork."*
- Preview the honesty pillars — near-miss routing and the 60-minute freshness rule — because both will be demonstrated later. That foreshadowing makes Slide 7 land.
- Transition: *"To believe that, you need to see who it's for."*

---

## Slide 3 — Target Users & Real-World Use Cases

**Title:** *Three Citizens, Three Schemes, One Screen*

### On-slide content

**Persona 1 — Ananya, 19 · 1st-yr B.Tech · Delhi · SC · ₹2.2 L household income**
- **Scheme:** Delhi Post-Matric Scholarship for SC/ST/OBC Students
  `sch_delhi_post_matric_scholarship_sc_st_obc_2026`
- **Value:** **₹48,000 tuition reimbursement + ₹12,000 maintenance = ₹60,000 / year**
- **Gates:** age 15–30 ✓ · Delhi domicile ✓ · household ≤ ₹2,50,000 ✓ · SC ∈ {SC, ST, OBC} ✓ → $S_{det} = 1$
- **Gap:** missing **SDM Income Certificate** (Tier 3, friction 0.20) — *but she already holds a self-declaration affidavit*
- **Bridge the Gap:** charged at **0.5 → P_docs = 0.10** → **Score 0.887 → HIGH (89%)**, checklist says *"start today; sanction needs the SDM certificate"*, `score_if_completed = 0.987`, with the e-District walkthrough.

**Persona 2 — Ramesh, 42 · small farmer · UP · 1.8 hectares**
- **Scheme:** PM-Kisan Samman Nidhi `sch_pm_kisan_samman_nidhi_2019`
- **Value:** **₹6,000 / year in 3 × ₹2,000 DBT**, Aadhaar-seeded account mandatory
- **Sole gap:** Tier-4 **land record (khasra/ROR)** → `P_docs = 0.30` → **Score 0.688 → MEDIUM**; get the land record → **0.988 → HIGH**

**Persona 3 — Priya, 27 · OBC · household ₹1,05,000 vs a ₹1,00,000 OBC ceiling**
- **Δ = 0.05 ≤ δ** → routed to **`NEAR_MISS` Borderline**, *not* shown as an 80% match.
- Copy shown: *"Family income exceeds the OBC ceiling by ₹5,000."*

**Also served:** college discovery cells, Common Service Centre operators, SHG/NGO field workers.

*Visual: three cards side-by-side, each with a band chip and a rupee figure.*

### Speaker notes

- Judges remember *people*, not architectures. Say the names.
- Ananya's card is the emotional centre: the missing SDM Income Certificate is the single most common reason a Delhi scholarship dies in an office queue — and we tell her she can **already start** with the affidavit she has.
- Ramesh shows the same engine on a completely different domain (DBT + landholding cap) — one system, no per-scheme hardcoding.
- Priya is the credibility move. **Every competitor will show the happy path. We show the rejection — with arithmetic.** Say it plainly: *"We will tell you you're five thousand rupees over, and we will show you the division."*
- If asked "how is this different from SarkariYojna blogs?": *"Those link to a page. We tell you whether the page is accepting applications today, and whether you — specifically you — clear every rule on it."*

---

## Slide 4 — Role of Machine Learning & AI

**Title:** *Why Neuro-Symbolic — and Why Not Just an LLM*

### On-slide content

**Three AI/ML components, each with a non-negotiable boundary:**

| Component | Method | What it does | Hard boundary |
|---|---|---|---|
| **Hybrid Retrieval** | `BAAI/bge-m3` 1024-dim dense vectors **+** BM25 (k1=1.2, b=0.75), fused by **Reciprocal Rank Fusion (k = 60)** | Dense captures *"engineering college fee support"*; sparse pins *"OBC"* and *"Delhi"* | Pure recall — decides nothing about eligibility |
| **Deterministic Rule Evaluator** | 7 hard gates + scheme-specific `domain_gates`, indicator product → **`S_det` ∈ {0, 1}** | Income ≤ ceiling (inclusive), age band, gender, domicile, category, education lattice, occupation | Pure function — same inputs, same answer, always |
| **LLM Semantic Auditor** | Judge over `soft_clauses[]` → $\lambda_{llm} \in \{1.0, 0.6, 0.2\}$ | Course accreditation, "discontinued student" clauses, exclusion classes (PM-Kisan income-tax payers) | **Can only down-weight.** Cannot un-fail a gate, cannot promote a scheme |

- **`S_sem` = clamp(λ_llm × S_hybrid, 0, 1)** — the LLM modulates relevance, never eligibility.
- **Document Friction Penalty `P_docs`** — a *learned-shaped* 5-tier model (0 → 0.35) converting missing paperwork into a score, capped at **0.60**.
- **LLM Structured Schema Parser** — schema-enforced extraction with **field-level provenance**; confidence < **0.75** → fail-closed to the Human Review Queue.
- **AI failure is designed for:** auditor timeout → $\lambda_{llm} := 1.0$; parse violation → HRQ, never partial index.

*Visual: a two-lane diagram — "what AI decides" vs "what AI may only suggest".*

### Speaker notes

- This is the slide that wins or loses a technical jury. Lead with the *limitation*, not the capability.
- The line to deliver slowly: **"A pure LLM will happily tell a ₹5-lakh-income family they qualify for a ₹2.5-lakh scheme — politely, confidently, and wrongly."** Deterministic gates exist so that can never happen.
- The inverse matters too: rules cannot read a scanned gazette. That is what the Semantic Auditor and the Structured Schema Parser are for.
- Explain $\lambda_{llm}$ in plain words: *"Three verdicts — matched, partial, no match — multiplying relevance by 1.0, 0.6 or 0.2. It has a hand on the dimmer switch, never on the on/off switch."*
- Mention provenance: every extracted number has to carry a verbatim quote from the source, or the scheme goes to human review. **That is how we defeat hallucination in civic data.**
- If asked about RRF vs concatenating scores: *"RRF is rank-based, so the dense and BM25 score distributions can drift without corrupting the fusion."*

---

## Slide 5 — Proposed Technical Approach

**Title:** *Layered, Async-First, One Source of Truth*

### On-slide content

```
Next.js 14 (App Router) + Tailwind + shadcn/ui
        │  POST /api/v1/profile/qualify
        ▼
FastAPI + Pydantic v2  ── Query Orchestrator (parallel fan-out, SSE)
        │
        ├─► Qdrant  (collection `schemes`, 1024-dim bge-m3, cosine)   [dense]
        ├─► In-memory BM25 index (k1 1.2, b 0.75)                      [sparse]
        ├─► MongoDB (schemes · scheme_revisions · verification_logs)   [truth]
        ├─► Neuro-Symbolic Scoring Engine (rules · auditor · penalty · assembler)
        └─► TinyFish Cloud Integration Gateway (Search · Fetch · Web Agent)
```

- **Storage contract:** MongoDB is canonical; Qdrant and BM25 are *derived, rebuildable* indexes. Nothing in Qdrant is authoritative.
- **Write ordering:** MongoDB commit → Qdrant upsert → BM25 rebuild → audit log.
- **Key endpoints:** `profile/qualify` · `schemes/{id}` · `verify/{id}` · `checklist/{id}` · `verify/stream/{job_id}` (SSE) · `admin/ingest/refresh`.
- **Modular services:** Ingestion Pipeline · Vector Retrieval Service · Rule Evaluation Service · TinyFish Integration Gateway.
- **No eligibility logic in the client.** Every ₹ figure and percentage is server-derived.

*Visual: the layered diagram from Phase 1 §2, simplified to four bands.*

### Speaker notes

- Emphasise **dependency direction**: dependencies point strictly downward; the Scoring Engine never talks to the browser, Storage never calls TinyFish. That is what makes the thing testable in 48 hours.
- The single most important architectural claim: **MongoDB is the only source of truth; the vector store is disposable.** If Qdrant dies we degrade to BM25-only — degraded, flagged, and still answering.
- Mention the clean separation that makes the demo credible: **eligibility is computed synchronously; verification streams in afterwards.** The citizen never waits on a government portal to find out if they qualify.
- If asked about ORM/framework choices: *"FastAPI gives us Pydantic-native contract enforcement — the same schema that validates our ingestion output validates our API surface."*
- One line on the frontend: *"The client renders; it never computes. That's a security property as much as a performance one."*

---

## Slide 6 — Proposed TinyFish Integration

**Title:** *Three TinyFish Tiers, Three Distinct Jobs*

### On-slide content

| | **Tier 1 — Search** | **Tier 2 — Fetch** | **Tier 3 — Web Agent** |
|---|---|---|---|
| **Endpoint / Infra** | `https://api.search.tinyfish.ai` | `https://api.fetch.tinyfish.ai` | Cloud browser via **Playwright / CDP** |
| **Question it answers** | *What schemes exist that we've never seen?* | *What does this page actually say?* | *Is this portal accepting applications **right now**?* |
| **Purpose** | **Discovery crawler** — newly notified gazettes, circulars, state portal announcements via targeted query templates | **Dynamic DOM rendering** — renders legacy **ASP.NET `__doPostBack`** pages and JS tables that kill `requests`+`BeautifulSoup`; returns token-efficient clean Markdown | **Live verification** — navigate → dismiss notice popups → detect *enabled* submit control → parse closing date → **viewport snapshot as proof** |
| **Feeds** | Ingestion Pipeline | Ingestion Pipeline → LLM Structured Schema Parser | Citizen query path (async, top-5) + freshness cron |
| **Latency budget** | **≤ 2,000 ms**/batch | **≤ 8,000 ms**/page | **≤ 4,000 ms**/portal (hard timeout) |
| **On the citizen request path?** | No | No | **Async only** (SSE) |
| **Failure fallback** | Sitemap/RSS polling | `fetch_unreachable` — **never index unrendered content** | `UNREACHABLE` / `BLOCKED` + `last_known_status` — eligibility unaffected |

- **Verdicts emitted:** `INTAKE_OPEN` · `INTAKE_CLOSED` · `UNREACHABLE` · `BLOCKED` · `UNVERIFIED` · `PENDING`
- **Evidence:** every check writes `verification_logs` + a snapshot at `snapshots/{scheme_id}/{job_id}.png`
- **Freshness SLA:** a "Verified" badge is only legal inside a **60-minute** window (Phase 1 G2).

*Visual: three vertical columns with the endpoint in bold at the top; arrow the Web Agent column straight into the dashboard card.*

### Speaker notes

- **This is the slide the TinyFish track judges score.** Name all three surfaces explicitly — Search, Fetch, Web Agent — and never let them blur into "we used an API".
- Tier 1 is *how we find schemes nobody linked to*. Tier 2 is *how we read pages that break scrapers*. Tier 3 is *how we know the link isn't dead*.
- The punchline for Tier 3: **"An aggregator blog links to a page. We link to a page, the deadline, and a screenshot of it, taken four minutes ago."**
- Walk through the agent's six visible actions in one breath: *navigate, dismiss the popup, check if Apply is actually enabled, read the closing date, snapshot, classify.*
- Stress the isolation: **"If all of TinyFish goes down, the eligibility answer is already computed and already correct. We degrade to 'last verified two days ago' — and we say exactly that."**
- Mention the SSRF guard: we only ever let the browser visit `gov.in` / `nic.in` hosts.

---

## Slide 7 — Expected Impact

**Title:** *From a Weekend of Portal-Hunting to 60 Seconds and One Verified Link*

### On-slide content

**Measured in our own test suite (50 acceptance tests, 10 invariants):**

| Dimension | Before SchemeRadar | With SchemeRadar |
|---|---|---|
| **Time to a trusted shortlist** | Hours–days across N portals | **≤ 60 s profile + ≤ 6,000 ms p95 verified list** |
| **Link freshness** | Blogs link to expired 2022/23 cycles | **Verified within 60 minutes**, or the badge says `UNVERIFIED` — we never fake it |
| **"Am I eligible?"** | Read a 40–60 page gazette, guess | **`Score` + `band` + full `gate_trace`** showing observed vs required per rule |
| **"What's actually missing?"** | *"Bring the usual documents"* | **Bridge the Gap Checklist** — ordered by score gain, each step with authority, guide URL and `score_if_completed` |
| **False hope** | Aggregators advertise schemes you fail | **`NEAR_MISS` routing** — shown separately with the exact violation (e.g. ₹5,000 over) |
| **Wrong claims** | Un-verified LLM output | **Fail-closed HRQ** — an unparseable scheme never reaches a citizen at all |

**Quantified friction reduction (worked examples):**
- Ananya: **0.887 → HIGH** with one self-declared alternative in hand; **0.987** achievable after one e-District step.
- Ramesh: one Tier-4 land record converts **0.688 → 0.988** — the checklist tells him *exactly* which document buys **+0.30**.
- Coverage: seed catalogue at demo; **design capacity** = full central + state catalogue via content-hash incremental ingestion (`target`).

*Visual: a two-column "before/after" table; bold every number.*

### Speaker notes

- Quantify *friction*, not vanity metrics. The most persuasive number on this slide is **+0.30** — we can tell Ramesh precisely which piece of paper is worth how much score. No competitor can do that.
- Reiterate the honesty metrics: `NEAR_MISS` routing and the 60-minute badge rule. Impact includes **not misleading people**.
- Be scrupulous about labels: the coverage figure is a *design capacity / target*, and say so out loud. A jury that catches you inflating one number disbelieves the rest.
- The fail-closed line is an impact statement, not just an engineering one: *"A scheme we cannot parse correctly is invisible rather than wrong. In civic tech, invisible beats wrong."*
- Close the slide with: *"The unclaimed ₹1.4 lakh crore doesn't need a new scheme. It needs a radar."*

---

## Slide 8 — Feasibility & Cost-Scalability

**Title:** *Built to Degrade, Priced to Run*

### On-slide content

**Latency budgets (p95):**

| Path | Budget |
|---|---|
| Sync eligibility response (validate → dense → sparse → fuse → rules → LLM audit → assemble) | **≤ 1,200 ms** |
| Fully verified payload via SSE | **≤ 6,000 ms** |
| Per-scheme ingestion (Search → Fetch → OCR → LLM parse → embed → upsert) | ≤ 20,000 ms, **parallelised**, async |

**Why it scales:**

- **Async-first:** Tier-3 browsers (top-5, concurrency 3) never sit on the eligibility path.
- **Cost-aware ingestion:** `content_hash` unchanged → parse skipped. LLM spend occurs **only on new/changed pages**.
- **Batched AI:** one Semantic Auditor call for the top-10 candidates at ≤ 900 ms — not 30 calls.
- **Fail-closed Human Review Queue (HRQ):** no garbage in the index ⇒ no support burden from wrong answers; parse-confidence floor 0.75, max 1 repair attempt.
- **Degradation matrix (designed, tested):** Qdrant down → BM25-only with `degraded` flag · LLM down → $\lambda_{llm} := 1.0$ · TinyFish down → `UNVERIFIED` + last-known status · **eligibility never blocked**.
- **Footprint:** runs single-node at hackathon scale; scales by adding a retrieval sidecar and a browser-worker pool. Snapshot objects auto-expire at **90 days**.

*Visual: the latency budget as a horizontal bar; the degradation matrix as a 4-row table.*

### Speaker notes

- Lead with the p95 numbers — **1,200 ms sync, 6,000 ms verified** — they are the two figures a jury can sanity-check.
- The architectural bet: *"We made the slow, expensive, unreliable thing — visiting a government portal — asynchronous, and the fast, deterministic thing synchronous."*
- Cost levers, in order of importance: content-hash skip (no re-parsing unchanged pages), top-10 batching for the LLM, top-5 verification per query, 90-day snapshot lifecycle.
- The degradation matrix is the feasibility answer nobody expects. Say it as a list: *"Each dependency has a designed, tested failure mode. None of them can produce a wrong eligibility answer."*
- On fail-closed: *"Every scheme that fails schema validation goes to a human queue instead of the index. That trades a little recall for zero misinformation — the correct trade in this domain."*

---

## Slide 9 — Future Scope

**Title:** *The Radar Gets Louder — 12-Month Horizon*

### On-slide content

| Horizon | Capability | Already scaffolded by |
|---|---|---|
| **0–3 months** | **DigiLocker integration** — auto-populate `documents_in_hand` from a citizen's issued documents | `documents_in_hand[]` is already a document-id set; `charged_weight` model instantly re-prices $P_{docs}$, collapsing Tier 2–4 friction to Tier 0–1 |
| **0–3 months** | **Deadline alerts** — SMS/WhatsApp nudge on `extracted_deadline` | `verification_logs` + `extracted_deadline` already persisted per scheme |
| **3–6 months** | **Regional-language voice agent** — Hindi-first, then Punjabi/Bengali/Telugu/Tamil | Schema already carries `name_hi`; `BAAI/bge-m3` is multilingual, so retrieval works in-script with **no re-indexing** |
| **3–6 months** | **Automated form-filling assistant** — the Tier-3 browser session pre-fills the application, **citizen reviews and submits** (never auto-submit) | Playwright/CDP session, `ProfileContext` and `required_documents[]` already model the form's inputs |
| **6–12 months** | **Institution / CSC console** — colleges and Common Service Centres bulk-verify their students | `ProfileContext` is account-optional and PII-minimised by design |
| **6–12 months** | **NGO / researcher API** + multilingual printable checklists | Frozen `Scheme` schema and versioned `schema_version = 1.0.0` |

- **Hard promises for the future:** no auto-submission without human confirmation; no storage of Aadhaar numbers — only possession booleans.

*Visual: a horizontal timeline with the four scaffold references as small chips.*

### Speaker notes

- The differentiator is that these are **already scaffolded**, not aspirational: `name_hi` is in the schema, `documents_in_hand` is a document-id set, the browser session already exists. Point at the scaffolding column.
- DigiLocker is the single highest-leverage item: it removes the *entire* Tier-3/Tier-4 friction problem by handing us the certificate instead of sending the citizen to the Tehsil.
- Voice/regional is the reach multiplier — India's beneficiaries are not English-speaking, and bge-m3 means we do not re-index to support them.
- Be explicit about the safety rails on form-filling: **we pre-fill, the human submits.** Say it before anyone asks.
- Close the deck on the civic thesis: *"Every future feature reduces the distance between 'I qualify' and 'I received it.'"*

---

## Timed 3-Minute Oral Pitch Script

**Day 2 Grand Finale · 180 seconds · ~450 spoken words**

> Stage directions are in *italics in brackets* and are **not** read aloud. Timing markers are cumulative.

---

**[0:00 – 0:25] · HOOK — Slide 1**
*(Walk to centre stage. Hold on the title card. Do not click yet.)*

Every year, India's governments allocate over **one point four lakh crore rupees** to welfare schemes — scholarships, fee support, farmer aid, healthcare.

More than **sixty-five percent** of the people those schemes were written for will never see a rupee of it.

Not because the scheme doesn't exist — because nobody could tell them, *reliably*, that it exists, that **they** qualify, and that the window is **still open**.

*(Click — Slide 1.)*

---

**[0:25 – 0:55] · PROBLEM — Slide 1**

Three walls.

**One — fragmentation.** Hundreds of unlinked state portals: Delhi e-District, Haryana Antyodaya-SARAL, the National Scholarship Portal. Crawlers die on their **legacy ASP.NET postbacks**.

**Two — legalese.** Eligibility sits in forty-to-sixty-page scanned gazette PDFs. *Creamy Layer exclusion*, *SDM-attested family ceiling* — written for lawyers, not for a mother paying a fee.

**Three — ghost links.** The blogs people trust are full of **expired 2022 deadlines**.

*(Click — Slide 2.)*

---

**[0:55 – 1:30] · SOLUTION — Slides 2 → 4**

**SchemeRadar** replaces the endless form with one question: *who are you?* Age, state, education, income, category — and which papers you already hold. Sixty seconds.

In under **one point two seconds**, **hybrid retrieval** fuses dense embeddings with BM25 by Reciprocal Rank Fusion, and the **neuro-symbolic engine** takes over.

Deterministic hard gates decide eligibility and can **never** be talked out of a rule. The LLM semantic auditor reads the fine print — but it only holds a dimmer switch, never the on-off switch. And a document-friction penalty tells you what is actually missing.

*(Click — Slide 4. Point to the formula on screen.)*

---

**[1:30 – 2:05] · TINYFISH — Slide 6**

*(Click — Slide 6.)*

Then **TinyFish**. Three tiers, three jobs.

The **Search API** finds newly notified gazettes our crawler would never find. The **Fetch API** renders the ASP.NET pages that kill ordinary scrapers, returning clean Markdown for schema-enforced parsing. And the **Web Agent** drives a real cloud browser — dismisses the notice popup, checks whether the **Apply button is actually enabled**, reads the closing date, and takes a **snapshot as proof**.

*(Click — Slide 3. Point to the verification badge on the dashboard.)*

Every link we show you was verified in the **last sixty minutes**. If it wasn't, we say so.

---

**[2:05 – 2:40] · PROOF — Slide 3**

Here is the honesty test.

**Ananya**, nineteen, B.Tech year one in Delhi: **sixty thousand rupees a year**. Every rule passes — but she lacks an SDM Income Certificate, and she *does* hold a self-declaration. So she does not queue first: **start today at eighty-nine percent, one e-District step takes it to ninety-nine.**

**Ramesh**, one-point-eight hectares, PM-Kisan. One document short — the land record — and the checklist tells him it is worth exactly **plus-zero-point-three-zero**.

A family earning **two lakh fifty-five thousand** against a **two lakh fifty thousand** ceiling? We do not round it kindly. *Borderline* — **five thousand over**.

When a portal goes down, the eligibility answer **doesn't move by a single point**.

*(Click — Slides 7 → 9.)*

---

**[2:40 – 3:00] · IMPACT + CLOSE — Slides 7 → 9**

**Fifty acceptance tests. Ten invariants we refuse to break.** Fail-closed indexing: a scheme we cannot parse correctly never reaches a citizen at all.

DigiLocker to kill paperwork friction. A Hindi voice agent. Form-filling — **we pre-fill, the human submits**.

The distance between *"I qualify"* and *"I received it"* is now **one screen instead of a weekend**.

Back **SchemeRadar**, and give every Indian the radar their own tax money already paid for. **Thank you.**

*(Pause. Stop on Slide 9.)*

---

### Pitch timing audit

| Segment | Window | Duration | Slides |
|---|---|---|---|
| Hook — unclaimed wealth | 0:00 – 0:25 | 25 s | 1 |
| Problem — three walls | 0:25 – 0:55 | 30 s | 1 |
| Solution — neuro-symbolic | 0:55 – 1:30 | 35 s | 2 → 4 |
| TinyFish — three tiers | 1:30 – 2:05 | 35 s | 6 |
| Proof — three citizens | 2:05 – 2:40 | 35 s | 3 |
| Impact + close | 2:40 – 3:00 | 20 s | 7 → 9 |
| **Total** | **0:00 – 3:00** | **180 s** | **1 → 9** |

**Delivery notes**

- **The hook must land before TinyFish is named** — problem first, solution second, per rubric order and per dramatic order.
- Slow down on every number. Speed reads as nervousness; *pauses* read as confidence.
- Practise the three-tier sentence until it is one breath: *Search to discover, Fetch to read, Agent to verify.*
- The three-citizen proof block is the applause line — leave a half-beat after *"five thousand rupees over."*
- Never say "we think", "probably", or "basically". If a number is in §10, state it flatly.
- If interrupted, protect these three beats in order: **(1) ₹1.4 lakh crore / 65%, (2) the three TinyFish tiers, (3) Ananya's 89%.** Everything else is recoverable.

---

## 10. Consistency Appendix — Every Number, Its Source

| Figure spoken or shown | Value | Source |
|---|---|---|
| Annual welfare allocation | ₹1.4 Lakh Crore+ | Concept spec · Phase 1 §1.1 |
| Non-access rate | 65%+ | Concept spec · Phase 1 §1.1 |
| Gazette length | 40–60 pages | Phase 1 §1.1 |
| Score formula / weights | $W_D = 0.60$, $W_S = 0.40$ | Phase 1 §6.1, §9 |
| Penalty cap | $P_{cap} = 0.60$ | Phase 1 §6.4.3, §9 |
| Document tier range | 0.00 → 0.35 | Phase 1 §6.4.1 · Phase 2 §2.5 |
| RRF smoothing | $k = 60$ | Phase 1 §6.3.1, §9 |
| Fused candidates to rules | top-30 | Phase 1 §9 |
| LLM audit candidates | top-10 | Phase 1 §9 |
| Verdict multipliers | $\lambda_{llm} \in \{1.0, 0.6, 0.2\}$ | Phase 1 §6.3.2 |
| Embedding model / dims | `BAAI/bge-m3`, 1024 | Phase 1 §5.3.1, §9 |
| BM25 params | k1 1.2, b 0.75 | Phase 1 §5.3.2, §9 |
| Hard gates | 7 (+ scheme `domain_gates`) | Phase 1 §6.2.1 · Phase 2 §0.3 |
| Parse-confidence floor | 0.75 | Phase 1 §3.1 · Phase 2 §7.4 |
| Sync p95 | 1,200 ms | Phase 1 §8.1 |
| Verified payload p95 | 6,000 ms | Phase 1 §8.1 |
| Tier-1 / Tier-2 / Tier-3 budgets | 2,000 / 8,000 / 4,000 ms | Phase 1 §7 |
| Verification freshness | 60 minutes | Phase 1 G2 · §9 |
| Top-N verified / browser concurrency | 5 / 3 | Phase 1 §7.3 |
| Snapshot lifecycle | 90 days | Phase 1 §7.3 |
| Delhi PMS scheme id | `sch_delhi_post_matric_scholarship_sc_st_obc_2026` | Phase 2 §4 |
| Delhi PMS value | ₹48,000 tuition + ₹12,000 maintenance = ₹60,000/yr | Phase 2 §4.1 |
| Delhi PMS income ceilings | ₹2,50,000 (SC/ST) · ₹1,00,000 (OBC override) | Phase 2 §4.1, §2.6 |
| Delhi PMS window | 01 Jun 2026 – 30 Nov 2026 | Phase 2 §4.1 |
| PM-Kisan scheme id | `sch_pm_kisan_samman_nidhi_2019` | Phase 2 §5 |
| PM-Kisan value | ₹6,000/yr = 3 × ₹2,000 DBT | Phase 2 §5.1 |
| PM-Kisan landholding cap | ≤ 2.0 ha | Phase 2 §5.1 (`dg_landholding_max`) |
| Ananya: $P_{docs}$ / Score / band | 0.10 / **0.887** / HIGH | Phase 3 §3.1 (Edge Case A) |
| Ananya: `score_if_completed` | 0.987 | Phase 3 §1.5 |
| Ananya without the affidavit | $P_{docs}$ 0.20 / 0.787 / MEDIUM | Phase 3 §3.1 |
| Ramesh: $P_{docs}$ / Score | 0.30 / **0.688** → 0.988 | Phase 2 §5.3 |
| Priya / borderline | ₹1,05,000 vs ₹1,00,000, Δ = 0.05 → `NEAR_MISS` | Phase 2 §4.3 |
| Borderline headline case | ₹2,55,000 vs ₹2,50,000, Δ = 0.02 | Phase 1 §6.4.3 · Phase 3 §3.2 |
| `NEAR_MISS` score | forced to `0` | Phase 1 §6.5 · Phase 3 §1.4.2 |
| Acceptance tests | 50 (TC-A1…A7, B1…B10, C1…C11, TC-01…22) | Phase 3 §3.4 |
| Invariants | 10 (I-1…I-10) | Phase 3 §3.5 |
| Verification verdicts | `INTAKE_OPEN` · `INTAKE_CLOSED` · `UNREACHABLE` · `BLOCKED` · `UNVERIFIED` · `PENDING` | Phase 1 §10 · Phase 2 §2.3 |
| Snapshot key | `snapshots/{scheme_id}/{job_id}.png` | Phase 1 §7.3 |
| Schema version | `1.0.0` | Phase 2 §2.1 |
| Event | AZINHACK '26, 21–22 Oct 2026, USAR GGSIPU East Delhi | Brief |

**Projections (must be labelled `target` on stage):** seed-catalogue size at demo · full central + state catalogue *design capacity* · any future DigiLocker/voice reach figures.

---

### Document Control

| Field | Value |
|-------|-------|
| Phase | 4 of 5 |
| File | `docs/PPT_SUBMISSION.md` |
| Precedent | Phases 1–3 (`ARCHITECTURE.md`, `DATA_SPEC.md`, `WORKFLOW_AND_TESTS.md`) |
| Next phase | Phase 5 — root `README.md` + `docs/BUILD_ORDER.md` |
| Deliverables | 9 slides with bulleted speaker notes + 180-second stage-directed pitch script + timing audit + number-provenance appendix |
