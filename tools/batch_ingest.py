"""Batch ingestion — Tier 1 → Tier 2 → Groq ladder → MongoDB/Qdrant.

Scope boundary (explicit): this tool runs the **ingestion** path only.  It never
invokes the Tier-3 Playwright web agent (ARCHITECTURE §7.3) — verification is a
separate, opt-in stage with its own 6000 ms budget and its own evidence trail.

Per seed the pipeline is:

1. **Idempotency** — query MongoDB first.  A query whose scheme already exists
   with ``index_state = "indexed"`` logs ``[SKIP]`` and costs no LLM tokens.
2. **Tier 1** — TinyFish search, one query, filtered to ``*.gov.in`` / ``*.nic.in``.
3. **Tier 2** — render candidates and keep the richest *parseable* page.  The
   scoring heuristic is imported from the live smoke so "what counts as a
   scheme page" has exactly one definition in the repo.
4. **Ladder** — ``ingest_markdown`` runs L1→L5 with one repair.  On ``ok`` it
   commits MongoDB then calls ``indexer`` (embed → Qdrant).  On ``human_review``
   it writes ``index_state = "pending_review"`` and provably never calls
   ``indexer`` — no vector, no Qdrant point.
5. **Throttle** — calls to Groq are paced 45 s apart and retried on 429, because
   the schema prompt plus completion is ~7.5k tokens against an 8k token/min cap.

``--dry-run`` executes discovery, fetch and the ladder but writes nothing to
MongoDB or Qdrant (it still spends LLM tokens — that is the point of the run).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

REPO = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_SEEDS_PATH = REPO / "data" / "seeds.json"

# Groq free tier = 8,000 tokens/min; one extraction is ~7.5k.  Pace every call.
# Groq's free tier grants ~8k *tokens per 60 s window*, not 8k per request.
# Pacing below the window means the next call still lands inside the previous
# one's budget and comes back HTTP 429 (measured: a 45 s gap produced 429 on
# nearly every call, including a page of only 6418 chars).  60 s puts one
# request per window, so rate limiting becomes the exception rather than the
# default.  Ingestion runs in the background, so wall-clock cost is free.
LLM_MIN_GAP_S = 60.0
# 429 always clears once the window rolls, so give it room to recover rather
# than declaring the scheme unparseable after a minute of contention.
LLM_RETRIES = 4

# An unhydrated SPA shell renders to ~220-260 chars (a sign-out prompt and a
# footer), so anything under this is a shell rather than a scheme page.  Set
# well above that floor but below real content: ministry and PIB pages land in
# the 3k-30k range.
MIN_RENDER_CHARS = 500

# Upper bound on source markdown sent to the LLM.  The binding constraint is
# prompt tokens: measured overhead is ~16.8k chars (15.4k of schema + rules),
# so ``prompt_tokens ≈ (16800 + md) / 4``.  A 9000-char page costs ~6456
# prompt tokens, leaving ~1.5k of the 8k window for the completion; 12000+
# chars costs ~7200 and reliably came back HTTP 429 before the completion
# could finish.  Pages past this are still rejected rather than parsed
# shallowly — the candidate selector simply moves to a smaller, still-valid
# page that carries its own scheme signals.
MAX_RENDER_CHARS = 9000

# Query terms that carry no discriminating power for idempotency matching.
# Two kinds: generic words ("scheme", "yojana") and *retrieval* modifiers the
# organic query phrasing adds on top of the scheme's identity — "eligibility",
# "documents", "vikaspedia" are how we asked the search engine for a page, not
# what the scheme is called.  Including them made the match require every token,
# so a query for "PM Kisan eligibility benefits documents vikaspedia" never
# matched the stored slug ``pm-kisan-samman-nidhi`` and re-ingested a duplicate.
_MATCH_STOPWORDS = {
    # generic
    "scheme", "yojana", "for", "of", "the", "and", "a", "an", "plan",
    "programme", "program", "gov", "in", "www",
    # retrieval modifiers
    "eligibility", "eligible", "benefit", "benefits", "document", "documents",
    "docs", "criterion", "criteria", "guideline", "guidelines", "detail",
    "details", "vikaspedia", "pib", "assistance", "financial", "income",
    "ceiling", "term", "terms", "component", "components", "entitlement",
    "category", "categories", "interest", "subsidy", "subsidies", "apply",
    "application", "check", "how",
}


class BatchIngestError(Exception):
    """Configuration, connectivity or manifest problem — not a per-seed failure."""


# ---------------------------------------------------------------------------
# Seed manifest
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SeedItem:
    index: int
    persona: str
    query: str


def load_seeds(path: pathlib.Path) -> list[SeedItem]:
    if not path.exists():
        raise BatchIngestError(f"seed manifest not found: {path}")
    try:
        body = json.loads(path.read_text(encoding="utf8"))
    except json.JSONDecodeError as exc:
        raise BatchIngestError(f"{path}: not valid JSON: {exc}") from exc
    raw = body.get("queries")
    if not isinstance(raw, list) or not raw:
        raise BatchIngestError(f"{path}: `queries` must be a non-empty array")
    items: list[SeedItem] = []
    for i, entry in enumerate(raw, 1):
        if not isinstance(entry, dict) or not str(entry.get("query", "")).strip():
            raise BatchIngestError(f"{path}: queries[{i - 1}] needs a non-empty `query`")
        items.append(
            SeedItem(
                index=i,
                persona=str(entry.get("persona") or "unassigned"),
                query=str(entry["query"]).strip(),
            )
        )
    return items


# ---------------------------------------------------------------------------
# Datastores
# ---------------------------------------------------------------------------
def _connect(settings):
    from pymongo import MongoClient
    from qdrant_client import QdrantClient

    mongo = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    try:
        mongo.admin.command("ping")
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool-level error
        raise BatchIngestError(f"MongoDB unreachable at {settings.mongo_uri}: {exc}") from exc
    db = mongo[settings.mongo_db]

    qdrant = QdrantClient(settings.qdrant_url, timeout=30)
    try:
        qdrant.get_collections()
    except Exception as exc:  # noqa: BLE001
        raise BatchIngestError(f"Qdrant unreachable at {settings.qdrant_url}: {exc}") from exc

    from services.api.storage import ensure_aux_collections, ensure_mongo_indexes

    ensure_mongo_indexes(db)
    ensure_aux_collections(db)
    return mongo, db, qdrant


def db_counts(db, qdrant_client, collection: str) -> dict[str, int]:
    return {
        "mongo_total": db.schemes.count_documents({}),
        "mongo_indexed": db.schemes.count_documents({"index_state": "indexed"}),
        "mongo_pending": db.schemes.count_documents({"index_state": "pending_review"}),
        "qdrant_points": int(qdrant_client.count(collection).count),
    }


# ---------------------------------------------------------------------------
# Idempotency — has this query already produced an indexed scheme?
# ---------------------------------------------------------------------------
def _match_terms(query: str) -> list[str]:
    """Significant tokens from a query, with the ``site:`` operator stripped."""
    without_operator = re.sub(r"\bsite:\S+", " ", query).lower()
    return [
        t for t in re.split(r"[^a-z0-9]+", without_operator)
        if t and t not in _MATCH_STOPWORDS and len(t) > 1
    ]


def find_existing_scheme_id(db, query: str) -> str | None:
    """Return ``scheme_id`` when this query already resolved to an indexed scheme.

    The query is the only thing we have *before* fetching, so the match is on
    ``slug`` / ``name``.  Two rules, both learned the hard way:

    * **Short tokens are ignored.** ``PM Fasal Bima Yojana`` has no ``pm`` token
      in the stored slug ``pradhan-mantri-fasal-bima-yojana``, so requiring every
      token re-ingested the same scheme under a second ``scheme_id``.
    * **Every remaining (length ≥ 3) token must appear as a whole word.** Keeping
      the match strict on the discriminating tokens is what stops
      ``Post Matric Scholarship SC`` from matching the OBC variant.

    Requiring the distinctive tokens biases towards missing a duplicate (cheap:
    one extra ingest) over skipping a scheme that was never ingested (expensive:
    a hole in the corpus).
    """
    terms = [t for t in _match_terms(query) if len(t) >= 3]
    if not terms:
        return None
    for doc in db.schemes.find(
        {"index_state": "indexed"}, {"scheme_id": 1, "slug": 1, "name": 1}
    ):
        haystack = f"{doc.get('slug') or ''} {doc.get('name') or ''}".lower()
        if all(re.search(rf"\b{re.escape(t)}\b", haystack) for t in terms):
            return str(doc["scheme_id"])
    return None


# ---------------------------------------------------------------------------
# Groq pacing — 429 backoff plus a fixed inter-call gap
# ---------------------------------------------------------------------------
# HTTP codes worth retrying rather than failing the seed.  413 was observed
# once and reproduced as HTTP 200 on the identical payload, so it is transient
# too.  5xx are upstream blips.
_TRANSIENT_HTTP_CODES = ("413", "429", "500", "502", "503", "504")

# Two distinct limits hide behind a single ``LLM HTTP 429``.  Groq's body tells
# which one we hit and, crucially, how long until *our own request size* fits:
# a per-minute window resolves in seconds, while a tokens-per-day budget needs
# the rolling 24 h allowance to drain (200000 / 86400 ≈ 2.3 tokens/s, so an
# 8.4k-token extraction can legitimately require ~45 min).  Honouring the
# server's own number is both faster and more accurate than guessing.
_LLM_HTTP_RE = re.compile(r"LLM HTTP (\d{3})")
_RETRY_AFTER_RE = re.compile(r"try again in\s+([0-9]+(?:\.[0-9]+)?)\s*s", re.I)
# Beyond this a "wait" is indistinguishable from a stalled job, so give up and
# let the seed land in the review queue instead of parking the batch for hours.
MAX_BACKOFF_S = 3600.0


def _status_code(exc: Exception) -> str | None:
    """Status from ``LLM HTTP <code>``, if the failure was an HTTP one.

    Matching only the code (not the whole message) stops digits *inside* the
    provider's detail text — ``Limit 200000`` — from being mistaken for a 500.
    """
    match = _LLM_HTTP_RE.search(str(exc))
    return match.group(1) if match else None


def _is_transient(exc: Exception) -> bool:
    code = _status_code(exc)
    if code is not None:
        return code in _TRANSIENT_HTTP_CODES
    return any(c in str(exc) for c in _TRANSIENT_HTTP_CODES)


def _retry_after_s(exc: Exception) -> float | None:
    """Provider-suggested delay in seconds, when it states one."""
    match = _RETRY_AFTER_RE.search(str(exc))
    if not match:
        return None
    return float(match.group(1))


class ThrottledLLM:
    """Wrap :func:`llm_extract` with a one-request-per-window floor and retries.

    ``ingest_markdown`` accepts the extractor as a parameter precisely so the
    transport policy can live outside the ladder: a rate limit is an operational
    concern, not a parse failure.
    """

    def __init__(self, inner: Callable[..., Any], gap_s: float = LLM_MIN_GAP_S,
                 retries: int = LLM_RETRIES) -> None:
        self._inner = inner
        self._gap_s = gap_s
        self._retries = retries
        self._last_call_at = 0.0
        self.calls = 0
        self.backoffs = 0

    async def __call__(self, markdown: str, **kwargs: Any) -> Any:
        from services.tinyfish.parser import LLMUnavailable

        wait = self._gap_s - (time.monotonic() - self._last_call_at)
        if self._last_call_at and wait > 0:
            await asyncio.sleep(wait)

        for attempt in range(self._retries + 1):
            self._last_call_at = time.monotonic()
            self.calls += 1
            try:
                return await self._inner(markdown, **kwargs)
            except LLMUnavailable as exc:
                if attempt >= self._retries or not _is_transient(exc):
                    raise
                # Prefer the provider's own arithmetic: it already accounts for
                # how big *this* request is.  Fall back to a widening multiple
                # of the inter-call gap when it does not say.
                delay = _retry_after_s(exc)
                if delay is None:
                    delay = self._gap_s * (attempt + 1)
                if delay > MAX_BACKOFF_S:
                    raise LLMUnavailable(
                        f"LLM budget exhausted; provider asks for {delay:.0f}s "
                        f"(capped at {MAX_BACKOFF_S:.0f}s): {exc}"
                    ) from exc
                delay = max(delay, min(self._gap_s, MAX_BACKOFF_S))
                self.backoffs += 1
                print(f"      [transient {str(exc).strip()[:70]}] backing off "
                      f"{delay:.0f}s (attempt {attempt + 1}/{self._retries})",
                      flush=True)
                await asyncio.sleep(delay)
        raise LLMUnavailable("transient LLM error persisted past the retry budget")  # pragma: no cover


# ---------------------------------------------------------------------------
# Tier 1 → Tier 2
# ---------------------------------------------------------------------------
def is_pdf_url(url: str) -> bool:
    """Skip PDFs before spending a render on them.

    Tier 2's renderer returns **zero characters** for PDFs (measured: both
    ``scholarships.gov.in`` guidelines and ``pmkisan.gov.in/RevisedFAQ.pdf``
    come back empty), and the scheme criteria inside them are scanned and
    multi-column anyway.  Filtering here rather than post-render keeps the
    render budget for HTML that can actually hydrate.
    """
    path = url.lower().split("?")[0]
    return ".pdf" in path or "/pdf/" in path


async def fetch_best_page(hits, fetcher):
    """Render discovered hits and return the richest parseable page.

    Three gates, in the order they cost money:

    1. **PDF** — dropped unrendered (``is_pdf_url``).
    2. **SPA shell** — anything under ``MIN_RENDER_CHARS`` is an unhydrated
       client-side app, not scheme content.
    3. **Parseable band** — enough scheme signals and a sane length, scored by
       the same heuristic the live smoke uses so both tools agree on what a
       usable scheme page looks like.

    Returns ``None`` when every hit is rejected; the caller logs that as
    ``[MISS] no_parseable_page`` and moves on — a dead source is data, not an
    error.
    """
    from tools.smoke_e2e_live import (
        IDEAL_CHARS,
        MAX_RENDER_CANDIDATES,
        SCHEME_MIN_SIGNALS,
        scheme_content_score,
    )

    best = None            # (score, closeness, result)
    for n, hit in enumerate(hits):
        url = str(hit.url)
        if is_pdf_url(url):
            print(f"      reject: {url} -> PDF (renderer returns 0 chars)", flush=True)
            continue
        if n >= MAX_RENDER_CANDIDATES:
            break
        try:
            cand = await fetcher.render(url)
        except Exception as exc:  # noqa: BLE001 - one bad hit must not abort the seed
            print(f"      render failed: {url} -> {type(exc).__name__}", flush=True)
            continue
        score = scheme_content_score(cand.markdown)
        print(f"      candidate: {url} -> {len(cand.markdown)} chars, "
              f"{score} scheme signals", flush=True)
        if len(cand.markdown) < MIN_RENDER_CHARS:
            print(f"      reject: {url} -> {len(cand.markdown)} chars "
                  f"(< {MIN_RENDER_CHARS}, unhydrated SPA shell)", flush=True)
            continue
        if len(cand.markdown) > MAX_RENDER_CHARS:
            print(f"      reject: {url} -> {len(cand.markdown)} chars "
                  f"(> {MAX_RENDER_CHARS}, crowds out the repair budget)", flush=True)
            continue
        if score < SCHEME_MIN_SIGNALS:
            print(f"      reject: {url} -> {score} scheme signals "
                  f"(< {SCHEME_MIN_SIGNALS} required)", flush=True)
            continue
        key = (score, -abs(len(cand.markdown) - IDEAL_CHARS))
        if best is None or key > best[0]:
            best = (key, cand)
        if score >= 5 and len(cand.markdown) >= IDEAL_CHARS:
            break
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Storage — embed on accept, never on HRQ
# ---------------------------------------------------------------------------
def embedding_text(model) -> str:
    """The dense-corpus string for an accepted scheme."""
    return (
        f"{model.name} - {model.department}. {model.benefits_summary}. "
        f"Eligibility: {model.eligibility_text}"
    )


def make_indexer(embedder, qdrant_client, collection: str) -> Callable[[Any], None]:
    from qdrant_client.models import PointStruct
    from services.api.storage import point_id, project_qdrant_payload

    def _indexer(model) -> None:
        vector = embedder.encode([embedding_text(model)])[0]
        qdrant_client.upsert(
            collection_name=collection,
            points=[
                PointStruct(
                    id=point_id(model.scheme_id),
                    vector=vector,
                    payload=project_qdrant_payload(model.model_dump(mode="json")),
                )
            ],
        )

    return _indexer


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
async def process_seed(
    item: SeedItem,
    *,
    db,
    qdrant_client,
    collection: str,
    embedder,
    llm: Callable[..., Any],
    dry_run: bool,
) -> dict[str, Any]:
    from services.tinyfish.client import TinyFishFetchClient, TinyFishSearchClient
    from services.tinyfish.parser import ingest_markdown

    record: dict[str, Any] = {
        "index": item.index, "persona": item.persona, "query": item.query,
        "result": "skipped", "scheme_id": None, "ms": 0,
    }
    started = time.perf_counter()

    existing = find_existing_scheme_id(db, item.query)
    if existing:
        record.update(result="skip_indexed", scheme_id=existing,
                      ms=int((time.perf_counter() - started) * 1000))
        return record

    # --- Tier 1 ----------------------------------------------------------
    async with TinyFishSearchClient() as search:
        hits = await search.discover([item.query], max_results=10)
    if not hits:
        record.update(result="no_hits", ms=int((time.perf_counter() - started) * 1000))
        return record
    print(f"      tier1: {len(hits)} gov hit(s)", flush=True)

    # --- Tier 2 ----------------------------------------------------------
    async with TinyFishFetchClient() as fetcher:
        page = await fetch_best_page(hits, fetcher)
    if page is None:
        record.update(result="no_parseable_page",
                      ms=int((time.perf_counter() - started) * 1000))
        return record
    print(f"      tier2: selected {page['final_url']} "
          f"({len(page.markdown)} chars)", flush=True)

    # --- Ladder + storage ------------------------------------------------
    indexer = None
    if not dry_run:
        indexer = make_indexer(embedder, qdrant_client, collection)

    outcome = await ingest_markdown(
        page.markdown,
        fetch={"final_url": page["final_url"], "content_hash": page.content_hash},
        db=None if dry_run else db,
        indexer=None if dry_run else indexer,
        llm=llm,
        check_dns=True,
    )
    record.update(
        result=outcome.result,
        scheme_id=outcome.scheme_id,
        index_state=outcome.index_state,
        violations=outcome.violations,
        layer=outcome.layer,
        name=outcome.scheme.name if outcome.scheme else None,
        ms=int((time.perf_counter() - started) * 1000),
    )
    return record


async def run_async(args: argparse.Namespace) -> int:
    from services.api.config import get_settings
    from services.api.embedding import Embedder
    from services.tinyfish.parser import llm_extract

    seeds = load_seeds(args.seeds)
    if args.limit is not None:
        seeds = seeds[: max(args.limit, 0)]
    if not seeds:
        print("nothing to process", file=sys.stderr)
        return 1

    settings = get_settings()
    _, db, qdrant_client = _connect(settings)
    collection = settings.qdrant_collection

    before = db_counts(db, qdrant_client, collection)
    mode = "DRY RUN (no writes)" if args.dry_run else "LIVE (writes enabled)"
    print(f"batch ingest — {len(seeds)} seed(s) — {mode}")
    print(f"  mongo before: {before['mongo_total']} docs "
          f"({before['mongo_indexed']} indexed / {before['mongo_pending']} pending) | "
          f"qdrant before: {before['qdrant_points']} points\n")

    # The 2.3 GB embedder is only worth loading when something will be written.
    embedder = None if args.dry_run else Embedder()

    llm = ThrottledLLM(llm_extract, gap_s=LLM_MIN_GAP_S, retries=LLM_RETRIES)
    tally: dict[str, int] = {}
    records: list[dict[str, Any]] = []
    wall_start = time.perf_counter()

    for i, item in enumerate(seeds):
        print(f"[{i + 1}/{len(seeds)}] {item.persona}: {item.query}", flush=True)
        try:
            rec = await process_seed(
                item, db=db, qdrant_client=qdrant_client, collection=collection,
                embedder=embedder, llm=llm, dry_run=args.dry_run,
            )
        except Exception as exc:  # noqa: BLE001 - a bad seed must not stop the batch
            rec = {"index": item.index, "persona": item.persona, "query": item.query,
                   "result": "error", "scheme_id": None, "ms": 0,
                   "error": f"{type(exc).__name__}: {exc}"}
        records.append(rec)
        tally[rec["result"]] = tally.get(rec["result"], 0) + 1

        tag = {
            "skip_indexed": "[SKIP]",
            "skipped": "[SKIP]",
            "ok": "[OK]",
            "repaired": "[OK]",
            "human_review": "[HRQ]",
            "rejected_content_gate": "[GATE]",
            "no_hits": "[MISS]",
            "no_parseable_page": "[MISS]",
            "error": "[FAIL]",
        }.get(rec["result"], "[?]")
        detail = rec.get("error") or rec.get("name") or rec.get("scheme_id") or ""
        if rec.get("violations"):
            detail = f"{detail} {rec['violations'][:2]}"
        print(f"    {tag} {rec['result']}  {rec['ms']} ms  {detail}", flush=True)

        # Groq pacing: keep every extraction ≥45 s from the previous one.
        if i < len(seeds) - 1 and rec["result"] not in {"skip_indexed", "skipped"}:
            print(f"    … pacing {LLM_MIN_GAP_S:.0f}s for Groq's token budget", flush=True)
            await asyncio.sleep(LLM_MIN_GAP_S)

    elapsed = time.perf_counter() - wall_start
    after = db_counts(db, qdrant_client, collection)

    print("\n" + "=" * 70)
    print("BATCH INGEST SUMMARY")
    print("=" * 70)
    for rec in records:
        print(f"  {rec['index']:>2}. {rec['persona']:<19} {rec['result']:<22} "
              f"{rec['ms']:>7} ms  {rec['query']}")
    print("-" * 70)
    for key in sorted(tally):
        print(f"  {key:<24} {tally[key]}")
    print("-" * 70)
    print(f"  mongo total        {before['mongo_total']:>4} -> {after['mongo_total']}")
    print(f"  mongo indexed      {before['mongo_indexed']:>4} -> {after['mongo_indexed']}")
    print(f"  mongo pending      {before['mongo_pending']:>4} -> {after['mongo_pending']}")
    print(f"  qdrant points      {before['qdrant_points']:>4} -> {after['qdrant_points']}")
    print(f"  llm calls          {llm.calls}  (rate-limit backoffs: {llm.backoffs})")
    print(f"  elapsed            {elapsed:.1f} s")
    if args.dry_run:
        print("  mode               dry run — nothing written")
    print("=" * 70)
    return 0


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.batch_ingest",
        description="Tier 1 → Tier 2 → Groq ladder → MongoDB/Qdrant (no Tier 3).",
    )
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="process only the first N seeds")
    parser.add_argument("--dry-run", action="store_true",
                        help="run discovery/fetch/ladder but write nothing")
    parser.add_argument("--seeds", type=pathlib.Path, default=DEFAULT_SEEDS_PATH,
                        help=f"seed manifest (default: {DEFAULT_SEEDS_PATH})")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run_async(args))
    except BatchIngestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
