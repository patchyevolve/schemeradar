"""Batch ingestion — Tier 1 → Tier 2 → configured LLM → MongoDB/Qdrant.

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
5. **Throttle** — calls are paced conservatively and honor the
   provider's retry-after hint, including rolling daily budget waits.

``--dry-run`` executes discovery, fetch and the ladder but writes nothing to
MongoDB or Qdrant (it still spends LLM tokens — that is the point of the run).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import httpx
import io
import json
import pathlib
import re
import sys
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Callable, Sequence
from urllib.parse import urljoin

REPO = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_SEEDS_PATH = REPO / "data" / "seeds.json"

# Free-tier model limits vary by project and model. Pace requests conservatively
# and honor provider retry hints; operators can tune this for their quota.
LLM_MIN_GAP_S = 60.0
# Free-tier requests can be throttled by per-minute or per-day quotas. Keep a
# bounded but overnight-capable retry budget; explicit provider waits win.
LLM_RETRIES = 48

# An unhydrated SPA shell renders to ~220-260 chars (a sign-out prompt and a
# footer), so anything under this is a shell rather than a scheme page.  Set
# well above that floor but below real content: ministry and PIB pages land in
# the 3k-30k range.
MIN_RENDER_CHARS = 500

# Source markdown budget. The extraction prompt has substantial fixed schema
# overhead, so a 9k page can still exceed a free-tier TPM limit once completion and
# hidden reasoning are counted. Keep source text small enough to leave room for
# those fixed costs; provenance is checked against exactly these delivered bytes.
MAX_RENDER_CHARS = 3500
DISCOVERY_SUFFIX = "official scheme eligibility benefits application guidelines FAQ"
MAX_COVERAGE_SEARCHES = 6
_COVERAGE_QUERY_TERMS = {
    "scheme_identity": "official scheme name overview",
    "eligibility": "eligibility criteria exclusions who can apply",
    "benefits": "benefit amount payment frequency official",
    "document_checklist": "documents required registration land records official",
    "application_route": "how to apply registration official portal process",
    "continuation": "latest current status continuation scheme period official",
}

# Fact categories a downstream eligibility scorer needs. We select text from
# across the page, rather than taking only the first N characters (which can
# be a navigation table, news banner, or document header on government sites).
_FACT_PATTERNS = (
    re.compile(r"\b(eligib|eligible|ineligible|criteria|qualif|beneficiar|landholding|domicile|residen|income|age limit|caste|category)\w*\b", re.I),
    re.compile(r"\b(benefit|amount|financial assistance|installment|instalment|subsid|grant|scholarship|pension|₹|rs\.?|rupees?|per year|annually)\b", re.I),
    re.compile(r"\b(apply|application|register|registration|enrol|enroll|portal|documents?|aadhaar|deadline|last date|opens? on|closes? on)\b", re.I),
    re.compile(r"\b(exclusion|not eligible|shall be|must be|will receive|provided|disbursed|credited|family|farmer|widow|disabilit)\w*\b", re.I),
    re.compile(r"\b\d[\d,]*(?:\.\d+)?\s*(?:/-|rupees?|rs\.?|per annum|per year|monthly|annually|crore|lakh)\b", re.I),
)


def prepare_extraction_text(markdown: str, max_chars: int = MAX_RENDER_CHARS) -> str:
    """Build a bounded, fact-focused excerpt from a noisy long government page.

    Keep original lines verbatim so extraction provenance can be validated
    against precisely the evidence sent to the model. Relevant lines are
    sampled across the full page, then restored to source order. Empty table
    shells and low-information boilerplate do not consume the token budget.
    """
    if max_chars <= 0 or not markdown:
        return ""
    lines = markdown.splitlines()
    rows: list[tuple[int, str, int]] = []
    for i, line in enumerate(lines):
        # TinyFish may put a whole PDF/press release into one markdown line.
        # Split that line into source-exact sentence fragments before ranking;
        # otherwise one 17k-character paragraph cannot fit in the budget.
        segments = re.split(r"(?<=[.!?।])\s+", line.strip()) if len(line) > max_chars else [line.strip()]
        fragments: list[str] = []
        for segment in segments:
            if len(segment) <= max_chars:
                fragments.append(segment)
                continue
            words = segment.split()
            chunk = ""
            for word in words:
                addition = (" " if chunk else "") + word
                if len(chunk) + len(addition) > max_chars and chunk:
                    fragments.append(chunk)
                    chunk = word
                else:
                    chunk += addition
            if chunk:
                fragments.append(chunk)
        for j, stripped in enumerate(fragments):
            if not stripped:
                continue
            visible = re.sub(r"[|\s`*_#~\-=.:]+", "", stripped)
            if not any(ch.isalnum() for ch in visible):
                continue
            # Fetch renderers often turn empty layout tables into dozens of pipes.
            if stripped.count("|") >= 4 and len(re.sub(r"[^\w₹]", "", stripped)) < 12:
                continue
            score = sum(bool(pattern.search(stripped)) for pattern in _FACT_PATTERNS)
            if len(stripped) < 24 and score == 0 and not stripped.startswith("#"):
                continue
            rows.append((i * 1000 + j, stripped, score))
    if not rows:
        return markdown[:max_chars]

    relevant = [row for row in rows if row[2] > 0]
    pool = relevant or rows
    # Divide the source into four regions and round-robin ranked lines so the
    # opening boilerplate cannot crowd out eligibility or application facts
    # that appear later in the page.
    buckets: list[list[tuple[int, str, int]]] = [[], [], [], []]
    last_line = max(1, rows[-1][0])
    for row in pool:
        bucket = min(3, row[0] * 4 // (last_line + 1))
        buckets[bucket].append(row)
    for bucket in buckets:
        bucket.sort(key=lambda row: (-row[2], row[0]))

    chosen: list[tuple[int, str, int]] = []
    used = 0
    depth = 0
    while True:
        added = False
        for bucket in buckets:
            if depth >= len(bucket):
                continue
            row = bucket[depth]
            cost = len(row[1]) + (1 if chosen else 0)
            if used + cost <= max_chars:
                chosen.append(row)
                used += cost
                added = True
        if not added:
            # If the next ranked lines do not fit, try the remaining shorter
            # lines before stopping; this preserves multiple fact categories.
            remaining = sorted(
                (row for bucket in buckets for row in bucket if row not in chosen),
                key=lambda row: (len(row[1]), -row[2], row[0]),
            )
            for row in remaining:
                cost = len(row[1]) + (1 if chosen else 0)
                if used + cost <= max_chars:
                    chosen.append(row)
                    used += cost
                    added = True
            if not added:
                break
        depth += 1

    excerpt = "\n".join(row[1] for row in sorted(chosen, key=lambda row: row[0]))
    return excerpt[:max_chars]


_COVERAGE_PATTERNS = {
    "eligibility": re.compile(
        r"\b(eligib\w*|beneficiar\w*|who can|criteria|qualif\w*|not eligible|must be|shall be)\b", re.I
    ),
    "benefits": re.compile(r"(?:₹|\b(?:rs\.?|inr|rupees?)\s*)\s*[\d,]+(?:\.\d+)?", re.I),
    "document_checklist": re.compile(
        r"\b(?:following|list of|required|supporting)\s+documents?\b|"
        r"\bdocuments?\s+(?:required|to be submitted|to be uploaded)\b|"
        r"application (?:shall|must) be accompanied by\b", re.I
    ),
    "individual_document_rule": re.compile(
        r"\b(?:aadhaar|identity proof|income certificate|caste certificate|bank passbook)"
        r"[^.\n]{0,100}\b(?:required|shall be collected|must be submitted|to be submitted)\b", re.I
    ),
    "application_route": re.compile(
        r"\b(?:apply|application|register|registration|enrol|enroll)\b[^.\n]{0,160}\b"
        r"(?:through|at|with|via|on|using)\b[^.\n]{0,100}\b"
        r"(?:portal|website|csc|office|patwar\w*|revenue official|bank|app|centre|center|institution)\b|"
        r"\bapplication portal\b", re.I
    ),
    "continuation": re.compile(
        r"\b(?:scheme|programme|program)\b[^.\n]{0,120}\b"
        r"(?:continue\w*|extension|extended|ongoing|valid up to|approved for|2025[-–]26|2026[-–]27)\b|"
        r"\b(?:extension of|continued until|approved for FY|scheme period)\b", re.I
    ),
}


def _coverage_matches(text: str, pattern: re.Pattern[str], limit: int = 2) -> list[str]:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return [line[:360] for line in lines if pattern.search(line)][:limit]


def assess_evidence_coverage(
    markdown: str, candidate_name: str | None = None
) -> dict[str, dict[str, Any]]:
    """Record exact passage signals; presence is not proof of support/completeness."""
    text = markdown or ""
    identity_tokens = sorted({
        token for token in re.findall(r"[a-z0-9]+", (candidate_name or "").lower())
        if len(token) >= 4 and token not in _IDENTITY_STOPWORDS
    }, key=len, reverse=True)
    identity_passages: list[str] = []
    for token in identity_tokens:
        identity_passages.extend(_coverage_matches(
            text, re.compile(rf"\b{re.escape(token)}\b", re.I), limit=1
        ))
        if len(identity_passages) >= 2:
            break
    evidence: dict[str, dict[str, Any]] = {
        "scheme_identity": {
            "status": "signal_found" if identity_passages else "not_found_in_packet",
            "matched_passages": identity_passages,
        }
    }
    for field, pattern in _COVERAGE_PATTERNS.items():
        matches = _coverage_matches(text, pattern, limit=12 if field == "continuation" else 2)
        if field == "continuation" and candidate_name:
            matches = [
                passage for passage in matches
                if _passage_names_candidate(candidate_name, passage)
            ]
            matches = matches[:2]
        status = "signal_found" if matches else "not_found_in_packet"
        evidence[field] = {"status": status, "matched_passages": matches}
    # A named document mentioned without an exhaustive checklist is useful
    # evidence, but it cannot satisfy required-document coverage.
    document_rule = evidence["individual_document_rule"]["matched_passages"]
    if document_rule and not evidence["document_checklist"]["matched_passages"]:
        evidence["document_checklist"]["status"] = "partial_rule_only"
        evidence["document_checklist"]["matched_passages"] = document_rule
    # Some PDF renderers flatten superscript footnote markers into money
    # amounts (e.g. "Rs.6000¹" -> "Rs.60001"). Do not let an exact quote
    # check turn that ambiguous token into a confident normalized benefit.
    currency_tokens = re.findall(r"(?:₹|\b(?:rs\.?|inr)\s*)([\d,]+)", text, re.I)
    ungrouped_large = [value for value in currency_tokens
                       if "," not in value and len(value) >= 5]
    if ungrouped_large and evidence["benefits"]["status"] == "signal_found":
        evidence["benefits"]["status"] = "needs_numeric_review"
        evidence["benefits"]["warnings"] = [
            "Un-grouped five-or-more-digit currency token; verify formatting/footnote against source."
        ]
    return evidence


def coverage_search_queries(
    coverage: dict[str, dict[str, Any]], candidate_name: str, seed_query: str,
    *, search_all_fields: bool = False,
) -> list[tuple[str, str]]:
    """Build sequential, field-specific TinyFish searches for evidence gaps.

    Search is only a discovery step; the returned URLs still have to pass the
    government-domain allowlist, render successfully, and contain the named
    scheme plus evidence for the requested field before entering the packet.
    """
    subject = candidate_name or seed_query
    queries: list[tuple[str, str]] = []
    for field, terms in _COVERAGE_QUERY_TERMS.items():
        status = coverage.get(field, {}).get("status")
        if status in {"signal_found", "checklist_passage_found"} and not search_all_fields:
            continue
        # A partial document rule or suspicious currency needs a better source.
        if field == "document_checklist" and status == "partial_rule_only":
            terms = "complete documents required checklist registration official"
        elif field == "document_checklist" and status == "needs_source_review":
            terms = "official required document checklist latest application guidelines"
        elif field == "benefits" and status == "needs_numeric_review":
            terms = "exact benefit amount annual instalments official latest"
        elif field == "benefits" and status == "needs_source_review":
            terms = "benefit amount eligibility category payment unit official guidelines"
        queries.append((field, f"{subject} {terms} government"))
        if len(queries) >= MAX_COVERAGE_SEARCHES:
            break
    return queries


def _coverage_status_is_useful(field: str, status: str | None) -> bool:
    if status in {"signal_found", "checklist_passage_found"}:
        return True
    # A single document rule is useful data but never represents an exhaustive
    # checklist. Keep the partial status visible for the parser and frontend.
    return field == "document_checklist" and status == "partial_rule_only"


def _apply_document_evidence_status(
    coverage: dict[str, dict[str, Any]], field_evidence: dict[str, dict[str, Any]],
) -> None:
    """Replace keyword-only document coverage with the conservative checklist result."""
    document = field_evidence.get("document_checklist") or {}
    status = document.get("status")
    if coverage.get("document_checklist", {}).get("status") == "needs_source_review":
        return
    if status == "checklist_passage_found":
        normalized = "checklist_passage_found"
    elif status == "partial_rule_only":
        normalized = "partial_rule_only"
    else:
        normalized = "not_found_in_packet"
    target = coverage.setdefault("document_checklist", {})
    target["status"] = normalized
    claims = document.get("claims") or []
    if claims:
        target["matched_passages"] = [claim.get("quote", "") for claim in claims[:2]]


def prepare_coverage_excerpt(
    markdown: str,
    max_chars: int,
    fields: Sequence[str],
    *,
    candidate_name: str | None = None,
    omit_suspicious_currency: bool = False,
) -> str:
    """Keep passages for unresolved fields ahead of generic page context."""
    if omit_suspicious_currency:
        kept_lines = []
        for line in (markdown or "").splitlines():
            segments = re.split(r"(?<=[.!?।])\s+", line)
            kept_lines.extend(
                segment for segment in segments
                if not re.search(
                    r"(?:₹|\b(?:rs\.?|inr)\s*)\s*\d{5,}(?![\d,])",
                    segment, re.I,
                )
            )
        markdown = "\n".join(kept_lines)
    rows: list[str] = []
    lines = [
        segment.strip()
        for line in (markdown or "").splitlines() if line.strip()
        for segment in re.split(r"(?<=[.!?।])\s+", line.strip())
        if segment.strip()
    ]
    field_order = (
        "continuation", "benefits", "eligibility", "application_route",
        "document_checklist", "scheme_identity",
    )
    for field in [name for name in field_order if name in fields]:
        pattern = _COVERAGE_PATTERNS.get(field)
        if not pattern:
            continue
        field_rows = [line for line in lines if pattern.search(line)]
        if field == "continuation" and candidate_name:
            field_rows = [
                line for line in field_rows
                if _passage_names_candidate(candidate_name, line)
            ]
        if field == "document_checklist" and not field_rows:
            # Keep a supported individual requirement even when a universal,
            # exhaustive checklist is absent. The coverage status remains
            # `partial_rule_only`, never `signal_found`.
            field_rows = [line for line in lines
                          if _COVERAGE_PATTERNS["individual_document_rule"].search(line)]
        for line in field_rows:
            if line not in rows:
                rows.append(line)
                break
    focused = "\n".join(rows)[:max_chars]
    if len(focused) >= max_chars:
        return focused
    remainder = prepare_extraction_text(markdown, max_chars - len(focused))
    combined = "\n".join(part for part in (focused, remainder) if part)
    return combined[:max_chars]


def resolve_cross_source_numeric_artifact(
    coverage: dict[str, dict[str, Any]], source_records: Sequence[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Resolve a malformed currency token only when two other sources agree.

    PDF text renderers can append a footnote marker to a number. This helper
    does not repair the token by itself: two distinct official source URLs must
    independently state the clean amount obtained by removing that final
    marker digit. The anomaly remains recorded as a warning.
    """
    benefit = coverage.get("benefits") or {}
    if benefit.get("status") != "needs_numeric_review":
        return coverage
    suspicious: set[str] = set()
    clean_sources: dict[str, set[str]] = {}
    for source in source_records:
        body = str(source.get("text") or source.get("evidence_excerpt") or "")
        url = str(source.get("url") or "")
        values = re.findall(r"(?:₹|\b(?:rs\.?|inr)\s*)([\d,]+)", body, re.I)
        for value in values:
            if "," not in value and len(value) >= 5:
                suspicious.add(value)
            else:
                normalized = value.replace(",", "")
                clean_sources.setdefault(normalized, set()).add(url)
    resolved = next((value for value in suspicious
                     if value.endswith("1") and len(clean_sources.get(value[:-1], set())) >= 2), None)
    if not resolved:
        return coverage
    result = {key: dict(value) for key, value in coverage.items()}
    result["benefits"] = dict(result["benefits"])
    result["benefits"]["status"] = "signal_found"
    clean_passages: list[str] = []
    canonical_amount = resolved[:-1]
    for source in source_records:
        body = str(source.get("text") or source.get("evidence_excerpt") or "")
        for line in body.splitlines():
            values = re.findall(r"(?:₹|\b(?:rs\.?|inr)\s*)([\d,]+)", line, re.I)
            normalized = [value.replace(",", "") for value in values]
            if canonical_amount in normalized and not any(
                "," not in value and len(value) >= 5 for value in values
            ):
                excerpt = line.strip()[:360]
                if excerpt and excerpt not in clean_passages:
                    clean_passages.append(excerpt)
                break
        if len(clean_passages) >= 2:
            break
    if clean_passages:
        result["benefits"]["matched_passages"] = clean_passages
    result["benefits"]["warnings"] = [
        f"Rendered source contained ambiguous Rs.{resolved}; two separate official sources "
        f"support Rs.{canonical_amount}. Keep the malformed rendering in the audit trail."
    ]
    return result


_IDENTITY_STOPWORDS = {
    "pradhan", "mantri", "yojana", "scheme", "national", "india", "of", "for",
    "and", "the", "mission", "programme", "program", "central",
}


def _passage_names_candidate(candidate_name: str, passage: str) -> bool:
    tokens = {
        token for token in re.findall(r"[a-z0-9]+", candidate_name.lower())
        if len(token) >= 4 and token not in _IDENTITY_STOPWORDS
    }
    haystack = (passage or "").lower()
    if {"kisan", "samman", "nidhi"}.issubset(tokens):
        return bool(re.search(r"\bpm[\s-]*kisan\b|\b(samman|nidhi)\b", haystack))
    return any(re.search(rf"\b{re.escape(token)}\b", haystack) for token in tokens)


def candidate_identity_matches(candidate_name: str | None, text: str) -> bool:
    """Require at least one distinctive candidate-name token in fetched text."""
    if not candidate_name:
        return True
    expected = {
        token for token in re.findall(r"[a-z0-9]+", candidate_name.lower())
        if len(token) >= 4 and token not in _IDENTITY_STOPWORDS
    }
    actual = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    return bool(expected and expected.intersection(actual))

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
    canonical_name: str | None = None
    catalog_status: str | None = None


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
    catalog_path = path.with_name("source_catalog.json")
    catalog_by_query: dict[str, dict[str, str]] = {}
    if catalog_path.exists():
        try:
            catalog = json.loads(catalog_path.read_text(encoding="utf8"))
            catalog_by_query = {
                str(row.get("seed_query")): {
                    "canonical_name": str(row.get("canonical_name") or ""),
                    "status": str(row.get("status") or ""),
                }
                for row in catalog.get("records", [])
                if row.get("seed_query")
            }
        except (json.JSONDecodeError, AttributeError, TypeError):
            # The seed manifest remains usable if its optional enrichment file
            # is absent or damaged; candidates then retain query-only identity.
            catalog_by_query = {}
    items: list[SeedItem] = []
    for i, entry in enumerate(raw, 1):
        if not isinstance(entry, dict) or not str(entry.get("query", "")).strip():
            raise BatchIngestError(f"{path}: queries[{i - 1}] needs a non-empty `query`")
        catalog_record = catalog_by_query.get(str(entry["query"]).strip(), {})
        items.append(
            SeedItem(
                index=i,
                persona=str(entry.get("persona") or "unassigned"),
                query=str(entry["query"]).strip(),
                canonical_name=catalog_record.get("canonical_name"),
                catalog_status=catalog_record.get("status"),
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
    # Exact seed-query provenance lives on accepted revisions. This handles
    # organic query wording that does not share every token with a scheme's
    # official title (for example, a named mission whose page title describes
    # its assistance rather than repeating the mission name).
    revision = db.scheme_revisions.find_one(
        {"seed_query": query}, sort=[("revision", -1)]
    )
    if revision and revision.get("scheme_id"):
        indexed = db.schemes.find_one(
            {"scheme_id": revision["scheme_id"], "index_state": "indexed"},
            {"scheme_id": 1},
        )
        if indexed:
            return str(indexed["scheme_id"])

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
# Provider pacing — 429 backoff plus a fixed inter-call gap
# ---------------------------------------------------------------------------
# HTTP codes worth retrying rather than failing the seed.  413 was observed
# once and reproduced as HTTP 200 on the identical payload, so it is transient
# too.  5xx are upstream blips.
_TRANSIENT_HTTP_CODES = ("413", "429", "500", "502", "503", "504")

# Provider error bodies vary; parse any advertised retry delay and otherwise
# use a conservative widening delay. Daily free-tier limits can require waits
# much longer than a single request window.
_LLM_HTTP_RE = re.compile(r"LLM HTTP (\d{3})")
# Groq states the wait as a Go-style duration — `1h4m32.448s`, `5m30s`, `43.2s`
# — not as a bare number of seconds.  Matching only `\d+s` silently returned
# None here, which dropped us back to a linear gap-derived backoff topping out
# around ten minutes: far short of the ~64 min the budget actually needed, so
# every attempt failed even though the provider had told us exactly when to
# come back.
_RETRY_AFTER_RE = re.compile(
    r"(?:try again|retry|retry after)\s+(?:in\s+)?([0-9hms.]+)", re.I
)
_RETRY_DELAY_RE = re.compile(r"retryDelay[\"']?\s*[:=]\s*[\"']([0-9hms.]+)", re.I)
_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?$", re.I)
# Honor the provider's wait hint. A rolling daily quota can require more than
# an hour; clamping it and retrying only four times guarantees premature HRQ.
MAX_BACKOFF_S = 86400.0
MAX_FALLBACK_BACKOFF_S = 3600.0


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


def _parse_duration(raw: str) -> float | None:
    """Go duration (``1h4m32.448s``) or bare seconds → seconds."""
    text = raw.strip().rstrip(".")
    if not text:
        return None
    match = _DURATION_RE.fullmatch(text)
    if match and any(match.groups()):
        return (
            float(match.group(1) or 0) * 3600
            + float(match.group(2) or 0) * 60
            + float(match.group(3) or 0)
        )
    try:
        return float(text)  # a bare number is already seconds
    except ValueError:
        return None


def _retry_after_s(exc: Exception) -> float | None:
    """Provider-suggested delay in seconds, when it states one."""
    match = _RETRY_AFTER_RE.search(str(exc))
    if match is None:
        match = _RETRY_DELAY_RE.search(str(exc))
    return _parse_duration(match.group(1)) if match else None


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
                # of the inter-call gap when it does not say. A bounded retry
                # count still prevents endless runs, while honoring a valid
                # daily-window hint avoids retrying before the quota clears.
                delay = _retry_after_s(exc)
                if delay is None:
                    delay = min(self._gap_s * (attempt + 1), MAX_FALLBACK_BACKOFF_S)
                delay = min(max(delay, min(self._gap_s, MAX_BACKOFF_S)), MAX_BACKOFF_S)
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
    """Whether a discovered URL looks like a PDF document."""
    path = url.lower().split("?")[0]
    return ".pdf" in path or "/pdf/" in path


async def fetch_official_pdf_text(url: str) -> tuple[str, str, str] | None:
    """Download and extract a small, text-based PDF from an allowlisted host.

    TinyFish remains the discovery/render path. This Tier 2 fallback handles
    official PDFs that TinyFish returns as an empty render. Redirects are
    re-validated, the body is capped, and scanned/image-only PDFs return no
    text so the ingestion ladder can fail closed.
    """
    from pypdf import PdfReader
    from services.tinyfish.client import assert_allowed_gov_url, assert_resolves_public

    current = assert_allowed_gov_url(url)
    payload = bytearray()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), follow_redirects=False) as client:
            for _ in range(4):
                assert_allowed_gov_url(current)
                assert_resolves_public(current)
                async with client.stream("GET", current, headers={"Accept": "application/pdf"}) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            return None
                        current = assert_allowed_gov_url(urljoin(current, location))
                        continue
                    if resp.status_code != 200:
                        return None
                    content_length = resp.headers.get("content-length")
                    if content_length and int(content_length) > 20 * 1024 * 1024:
                        return None
                    async for chunk in resp.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > 20 * 1024 * 1024:
                            return None
                break
            else:
                return None
    except (httpx.HTTPError, OSError, ValueError):
        return None

    if not payload.startswith(b"%PDF-"):
        return None
    try:
        reader = PdfReader(io.BytesIO(payload), strict=False)
        excerpts: list[str] = []
        for index, page in enumerate(reader.pages[:80], start=1):
            try:
                text = page.extract_text() or ""
            except Exception:  # malformed or unsupported page encoding
                continue
            if text.strip():
                excerpts.append(f"[Page {index}]\n{text.strip()}")
        markdown = "\n\n".join(excerpts)
    except Exception:  # malformed, encrypted, or unsupported PDF
        return None
    if len(markdown) < MIN_RENDER_CHARS:
        return None
    return markdown, current, hashlib.sha256(payload).hexdigest()


async def fetch_best_page(
    hits,
    fetcher,
    candidate_name: str | None = None,
    *,
    max_bundle_sources: int = 3,
):
    """Render discovered hits and return the richest parseable page.

    Two content gates:

    1. **SPA shell** — anything under ``MIN_RENDER_CHARS`` is an unhydrated
       client-side app, not scheme content.
    2. **Parseable band** — enough scheme signals and a sane length, scored by
       the same heuristic the live smoke uses so both tools agree on what a
       usable scheme page looks like.

    Oversized pages are **truncated, not rejected**: a 29k-char PIB press
    release is routinely the best source for a scheme, and dropping it on
    length alone turned several seeds into ``[MISS]``.  Truncation is safe
    because :func:`ingest_markdown` validates provenance against the exact
    bytes handed to the model, so nothing past the cut can be claimed.

    Returns ``(page, delivered_markdown)``, or ``None`` when every hit is
    rejected; the caller logs that as ``[MISS] no_parseable_page`` and moves
    on — a dead source is data, not an error.
    """
    from tools.smoke_e2e_live import (
        IDEAL_CHARS,
        MAX_RENDER_CANDIDATES,
        SCHEME_MIN_SIGNALS,
        scheme_content_score,
    )

    def hit_kind(hit: Any) -> str:
        url = getattr(hit, "url", None) or hit.get("source_url") or hit.get("url") or ""
        identity = (str(url) + " " + str(hit.get("title") or "")).lower()
        if "faq" in identity or "frequently" in identity:
            return "faq"
        if "guideline" in identity or "operational" in identity:
            return "guidelines"
        if re.search(r"current|latest|extension|continued|status|revised|fy\s*20", identity):
            return "current_update"
        if "application" in identity or "registration" in identity:
            return "application"
        if "pressrelease" in identity or "press release" in identity:
            return "press_release"
        path = re.sub(r"^https?://[^/]+", "", str(url)).rstrip("/")
        if not path or "/landing/scheme/detail/" in str(url).lower():
            return "official_overview"
        return "official_page"

    def hit_priority(hit: Any) -> int:
        identity = (str(hit.url) + " " + str(hit.get("title") or "")).lower()
        return (
            6 * bool(re.search(r"guideline|operational", identity))
            + 5 * bool(re.search(r"faq|frequently", identity))
            + 4 * bool(re.search(r"current|latest|extension|continued|status|revised|fy\s*20", identity))
            + 3 * bool(re.search(r"application|registration", identity))
            + 2 * bool(re.search(r"pressrelease|press release", identity))
            + 1 * bool(re.search(r"\.pdf(?:$|[?#])", identity))
        )

    # Prefer governing documents and FAQs over ministry homepages, and avoid
    # spending a render slot on multiple mirrors of the same document class.
    ranked_hits = sorted(hits, key=lambda hit: hit_priority(hit), reverse=True)
    diverse_hits = []
    seen_kinds: set[str] = set()
    seen_urls: set[str] = set()
    for hit in ranked_hits:
        kind = hit_kind(hit)
        url = str(hit.url)
        if url in seen_urls or (kind in seen_kinds and kind != "official_page"):
            continue
        diverse_hits.append(hit)
        seen_urls.add(url)
        seen_kinds.add(kind)
    hits = diverse_hits

    candidates: list[tuple[tuple[int, int, int], Any, str]] = []
    for n, hit in enumerate(hits):
        url = str(hit.url)
        if n >= MAX_RENDER_CANDIDATES:
            break
        cand = None
        try:
            cand = await fetcher.render(
                url,
                include_links=True,
                include_iframe_links=True,
                purpose=("Official scheme eligibility, benefits, application process, "
                         "operative guidelines, amendments, and official portals"),
            )
        except Exception as exc:  # noqa: BLE001 - one bad hit must not abort the seed
            print(f"      render failed: {url} -> {type(exc).__name__}", flush=True)
        if (cand is None or len(cand.markdown) < MIN_RENDER_CHARS) and is_pdf_url(url):
            extracted = await fetch_official_pdf_text(url)
            if extracted:
                from services.tinyfish.client import FetchResult

                pdf_text, final_url, content_hash = extracted
                cand = FetchResult({
                    "markdown": pdf_text,
                    "source_url": url,
                    "final_url": final_url,
                    "http_status": 200,
                    "rendered_at": datetime.now(timezone.utc).isoformat(),
                    "content_hash": content_hash,
                    "links": [],
                    "iframe_links": [],
                    "title": hit.get("title"),
                    "extraction_method": "pypdf_text",
                })
                print(f"      PDF fallback: {url} -> {len(pdf_text)} text chars", flush=True)
        if cand is None:
            continue
        text = cand.markdown
        score = scheme_content_score(text)
        print(f"      candidate: {url} -> {len(text)} chars, "
              f"{score} scheme signals", flush=True)
        if len(text) < MIN_RENDER_CHARS:
            print(f"      reject: {url} -> {len(text)} chars "
                  f"(< {MIN_RENDER_CHARS}, unhydrated SPA shell)", flush=True)
            continue
        # Select bounded evidence across the page; official sites often render
        # layout tables and boilerplate before the eligibility section.
        sent = prepare_extraction_text(text)
        if len(text) > MAX_RENDER_CHARS:
            print(f"      truncate: {url} -> {len(text)} -> {len(sent)} chars "
                  f"(prompt budget)", flush=True)
        if score < SCHEME_MIN_SIGNALS:
            print(f"      reject: {url} -> {score} scheme signals "
                  f"(< {SCHEME_MIN_SIGNALS} required)", flush=True)
            continue
        if not candidate_identity_matches(candidate_name, f"{cand.get('title') or ''}\n{text}"):
            print(f"      reject: {url} -> no identity signal for {candidate_name!r}", flush=True)
            continue
        source_quality = len(re.findall(
            r"guideline|faq|operational|eligib|scheme|\.pdf|notification",
            url + " " + str(cand.get("title") or ""), re.I,
        ))
        key = (source_quality, score, -abs(len(sent) - IDEAL_CHARS))
        candidates.append((key, cand, sent))
        # Continue through the bounded candidates so a recent official
        # overview/update can accompany the operative rules and FAQ.
    if not candidates:
        return None
    candidates.sort(key=lambda row: row[0], reverse=True)
    page = candidates[0][1]
    # Search results frequently return the governing PDF, FAQ, and current
    # ministry page separately. Keep a small, diverse official bundle instead
    # of allowing one result to stand in for all scheme rules.
    selected = [candidates[0]]
    selected_hashes = {candidates[0][1].content_hash}
    selected_urls = {str(candidates[0][1].get("source_url") or candidates[0][1].get("url") or "")}
    selected_kinds = {hit_kind(candidates[0][1])}
    for candidate in candidates[1:]:
        candidate_page = candidate[1]
        url = str(candidate_page.get("source_url") or candidate_page.get("url") or "")
        kind = hit_kind(candidate_page)
        if (candidate_page.content_hash in selected_hashes or url in selected_urls
                or (kind in selected_kinds and any(k != "official_page" for k in selected_kinds))):
            continue
        selected.append(candidate)
        selected_hashes.add(candidate_page.content_hash)
        selected_urls.add(url)
        selected_kinds.add(kind)
        if len(selected) >= max(1, max_bundle_sources):
            break
    sources = [{
        "url": str(candidate_page.get("source_url") or candidate_page.get("url") or ""),
        "fetched_url": str(candidate_page.get("final_url") or ""),
        "content_hash": candidate_page.content_hash,
        "title": candidate_page.get("title"),
        "published_date": candidate_page.get("published_date"),
        "role": hit_kind(candidate_page),
        "fetched_at": candidate_page.get("rendered_at"),
        "extraction_method": candidate_page.get("extraction_method") or "tinyfish_render",
        "visible_chars": len(candidate_page.markdown),
        "text": candidate_page.markdown,
    } for _, candidate_page, _ in selected]
    # Keep the full fetched text transiently so a later coverage-gap pass can
    # reselect exact passages from the new sources rather than repeatedly
    # shrinking an already truncated excerpt.
    page["_full_evidence_sources"] = sources

    # Follow a small set of official rule/application links from the selected
    # page. This bounded, one-hop crawl gives the extractor the operative
    # guideline/FAQ context that a homepage or press release often omits.
    link_candidates: dict[str, int] = {}
    link_pattern = re.compile(
        r"guideline|eligib|faq|benefit|scheme|operational|document|\.pdf|"
        r"application|apply|registration|kisan|yojana|rule|notification",
        re.I,
    )
    primary_url = str(page.get("source_url") or page.get("url") or "")
    for raw_link in [*(page.get("links") or []), *(page.get("iframe_links") or [])]:
        link = str(raw_link or "").strip()
        if not link or link == primary_url:
            continue
        try:
            from services.tinyfish.client import assert_allowed_gov_url
            assert_allowed_gov_url(link)
        except Exception:
            continue
        score = len(link_pattern.findall(link))
        if score:
            link_candidates[link] = max(score, link_candidates.get(link, 0))

    link_budget = max(0, max(1, max_bundle_sources) - len(sources))
    for link, _score in sorted(link_candidates.items(), key=lambda item: (-item[1], item[0]))[:link_budget]:
        try:
            supporting = await fetcher.render(
                link,
                include_links=False,
                purpose=("Official source for scheme eligibility, benefits, "
                         "application, or operative guidelines"),
            )
        except Exception as exc:  # noqa: BLE001 - the primary source may still be useful
            print(f"      supporting source failed: {link} -> {type(exc).__name__}", flush=True)
            continue
        support_text = supporting.markdown
        if len(support_text) < MIN_RENDER_CHARS:
            print(f"      supporting source skipped: {link} -> {len(support_text)} chars", flush=True)
            continue
        sources.append({
            "url": str(supporting.get("source_url") or link),
            "fetched_url": str(supporting.get("final_url") or ""),
            "content_hash": supporting.content_hash,
            "title": supporting.get("title"),
            "published_date": supporting.get("published_date"),
            "role": "official_supporting_page",
            "fetched_at": supporting.get("rendered_at"),
            "extraction_method": supporting.get("extraction_method") or "tinyfish_render",
            "visible_chars": len(support_text),
            "text": support_text,
        })
        print(f"      supporting source: {link} -> {len(support_text)} chars", flush=True)

    # Give every source room within the extraction budget; prepare_extraction_text
    # selects relevant passages from the whole page and preserves source quotes.
    excerpts: list[str] = []
    evidence_sources: list[dict[str, Any]] = []
    remaining = MAX_RENDER_CHARS
    for index, source in enumerate(sources):
        slots = len(sources) - index
        allowance = remaining // slots
        excerpt = prepare_extraction_text(source["text"], allowance)
        if excerpt:
            excerpts.append(f"SOURCE URL: {source['url']}\n{excerpt}")
            evidence_sources.append({
                "url": source["url"],
                "fetched_url": source.get("fetched_url"),
                "content_hash": source["content_hash"],
                "title": source.get("title"),
                "role": source.get("role"),
                "fetched_at": source.get("fetched_at"),
                "extraction_method": source.get("extraction_method"),
                "visible_chars": source.get("visible_chars"),
                "evidence_excerpt": excerpt,
            })
            remaining = max(0, remaining - len(excerpt) - len(source["url"]) - 12)
    page["evidence_sources"] = evidence_sources
    delivered = "\n\n".join(excerpts)[:MAX_RENDER_CHARS]
    page["evidence_sources"] = evidence_sources
    # Do not count URL/header metadata as evidence signals.
    page["evidence_coverage"] = assess_evidence_coverage(
        "\n".join(source["evidence_excerpt"] for source in evidence_sources),
        candidate_name,
    )
    print(f"      source bundle: {len(evidence_sources)} official page(s), "
          f"{len(delivered)} chars sent", flush=True)
    return page, delivered


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
    llm: Callable[..., Any] | None,
    dry_run: bool,
    coverage_only: bool = False,
    full_field_search: bool = False,
) -> dict[str, Any]:
    from services.tinyfish.client import TinyFishFetchClient, TinyFishSearchClient
    if not coverage_only:
        from services.tinyfish.parser import ingest_markdown

    record: dict[str, Any] = {
        "index": item.index, "persona": item.persona, "query": item.query,
        "candidate_name": item.canonical_name,
        "catalog_status": item.catalog_status,
        "result": "skipped", "scheme_id": None, "ms": 0,
    }
    started = time.perf_counter()

    existing = find_existing_scheme_id(db, item.query) if db is not None else None
    if existing:
        record.update(result="skip_indexed", scheme_id=existing,
                      ms=int((time.perf_counter() - started) * 1000))
        return record

    if item.catalog_status and item.catalog_status != "source_ready":
        record.update(
            result="candidate_held_by_source_review",
            ms=int((time.perf_counter() - started) * 1000),
        )
        return record

    # --- Tier 1 ----------------------------------------------------------
    discovery_query = f"{item.query} {DISCOVERY_SUFFIX}"
    gap_searches: list[dict[str, Any]] = []
    async with TinyFishSearchClient() as search, TinyFishFetchClient() as fetcher:
        hits = await search.discover([discovery_query], max_results=10)
        if not hits:
            record.update(result="no_hits", ms=int((time.perf_counter() - started) * 1000))
            return record
        print(f"      tier1: {len(hits)} gov hit(s)", flush=True)
        record["discovery_query"] = discovery_query

        selected = await fetch_best_page(hits, fetcher, item.canonical_name)
        if selected is None:
            record.update(result="no_parseable_page",
                          ms=int((time.perf_counter() - started) * 1000))
            return record
        page, source_md = selected
        sources = list(page.get("_full_evidence_sources") or [])
        seen_urls = {str(source.get("url") or "") for source in sources}
        coverage = page.get("evidence_coverage") or assess_evidence_coverage(
            source_md, item.canonical_name
        )

        from services.tinyfish.conflicts import review_source_conflicts
        initial_conflicts = review_source_conflicts(sources)
        if initial_conflicts["benefits"]["status"] in {
            "conflicting", "multiple_unclassified_claims", "no_comparable_numeric_claim",
        } and coverage.get("benefits", {}).get("status") in {
            "signal_found", "needs_numeric_review",
        }:
            coverage.setdefault("benefits", {})["status"] = "needs_source_review"
        if initial_conflicts["documents"]["status"] == "conflicting":
            coverage.setdefault("document_checklist", {})["status"] = "needs_source_review"

        from services.tinyfish.field_evidence import build_field_evidence

        initial_field_evidence = build_field_evidence(sources, item.canonical_name)
        _apply_document_evidence_status(coverage, initial_field_evidence)

        # Coverage gaps become narrow Tier-1 queries. Each query and its fetch
        # is awaited serially to avoid multiplying TinyFish concurrency or
        # spending on fields already supported by the first source bundle.
        for field, query in coverage_search_queries(
            coverage, item.canonical_name, item.query,
            search_all_fields=full_field_search,
        ):
            try:
                extra_hits = await search.discover([query], max_results=8)
            except Exception as exc:  # noqa: BLE001 - preserve the first packet
                gap_searches.append({"field": field, "query": query,
                                     "result": f"search_error:{type(exc).__name__}"})
                continue
            extra_hits = [hit for hit in extra_hits if str(hit.url) not in seen_urls]
            if not extra_hits:
                gap_searches.append({"field": field, "query": query,
                                     "result": "no_new_official_hits"})
                continue
            extra = await fetch_best_page(
                extra_hits, fetcher, item.canonical_name, max_bundle_sources=1
            )
            if extra is None:
                gap_searches.append({"field": field, "query": query,
                                     "result": "no_parseable_source"})
                continue
            extra_page, _extra_md = extra
            extra_sources = list(extra_page.get("_full_evidence_sources") or [])
            extra_coverage = assess_evidence_coverage(
                "\n".join(str(source.get("text") or "") for source in extra_sources),
                item.canonical_name,
            )
            extra_status = (extra_coverage.get(field) or {}).get("status")
            useful = _coverage_status_is_useful(field, extra_status)
            gap_searches.append({
                "field": field, "query": query, "result": "evidence_added" if useful else "no_field_evidence",
                "field_status": extra_status,
                "source_url": (extra_sources[0].get("url") if extra_sources else None),
            })
            if not useful:
                continue
            for source in extra_sources:
                url = str(source.get("url") or "")
                if url and url not in seen_urls:
                    source = dict(source)
                    source["retrieved_for"] = field
                    source["search_query"] = query
                    sources.append(source)
                    seen_urls.add(url)

        if gap_searches:
            # Preserve a compact, balanced packet for parsing while keeping
            # every quote traceable to its original source URL and hash.
            sources = sources[:9 if full_field_search else 6]
            raw_coverage = resolve_cross_source_numeric_artifact(
                assess_evidence_coverage(
                    "\n".join(str(source.get("text") or "") for source in sources),
                    item.canonical_name,
                ),
                sources,
            )
            numeric_reconciled = raw_coverage.get("benefits", {}).get("status") == "signal_found" \
                and bool(raw_coverage.get("benefits", {}).get("warnings"))
            source_overhead = sum(len(str(s.get("url") or "")) + 12 for s in sources)
            evidence_budget = 12000 if full_field_search else MAX_RENDER_CHARS
            per_source = max(180, (evidence_budget - source_overhead) // max(1, len(sources)))
            excerpts = []
            evidence_sources = []
            for source in sources:
                excerpt = prepare_coverage_excerpt(
                    str(source.get("text") or ""), per_source,
                    tuple(_COVERAGE_QUERY_TERMS),
                    candidate_name=item.canonical_name,
                    omit_suspicious_currency=numeric_reconciled,
                )
                if not excerpt:
                    continue
                excerpts.append(f"SOURCE URL: {source.get('url', '')}\n{excerpt}")
                kept = dict(source)
                kept["evidence_excerpt"] = excerpt
                kept.pop("text", None)
                evidence_sources.append(kept)
            page["evidence_sources"] = evidence_sources
            page["evidence_coverage"] = resolve_cross_source_numeric_artifact(
                assess_evidence_coverage(
                "\n".join(s["evidence_excerpt"] for s in evidence_sources),
                item.canonical_name,
                ),
                sources,
            )
            if numeric_reconciled:
                page["evidence_coverage"]["benefits"] = raw_coverage["benefits"]
            page["coverage_searches"] = gap_searches
            source_md = "\n\n".join(excerpts)[:evidence_budget]
            coverage = page["evidence_coverage"]

    # Compare only claims with a shared explicit unit. This is a conservative
    # code-only check: it can block a disputed field, but never choose which
    # source is right. The full claim evidence is retained in the coverage
    # report; unrelated amounts with different units are not called conflicts.
    conflict_review = review_source_conflicts(sources)
    artifact_warnings = coverage.get("benefits", {}).get("warnings", [])
    artifact_reconciled = any(
        "two separate official sources support" in str(warning).lower()
        for warning in artifact_warnings
    )
    if artifact_reconciled and conflict_review["benefits"]["status"] == "conflicting":
        # Preserve the narrow repair for a flattened PDF footnote: two
        # independent clean sources already resolved that exact artifact.
        conflict_review["benefits"]["status"] = "corroborated_after_rendering_artifact"
        conflict_review["benefits"]["resolution"] = (
            "Malformed rendered amount retained in audit; two clean official sources agree."
        )
    record["conflict_review"] = conflict_review
    if conflict_review["benefits"]["status"] in {
        "conflicting", "multiple_unclassified_claims", "no_comparable_numeric_claim",
    } and coverage.get("benefits", {}).get("status") in {
        "signal_found", "needs_numeric_review",
    }:
        coverage.setdefault("benefits", {})["status"] = "needs_source_review"
        coverage["benefits"].setdefault("warnings", []).append(
            "Benefit amounts conflict, have different/unclear scopes, or lack a beneficiary payment unit; amount withheld."
        )
    if conflict_review["documents"]["status"] == "conflicting":
        coverage.setdefault("document_checklist", {})["status"] = "needs_source_review"
        coverage["document_checklist"].setdefault("warnings", []).append(
            "Official sources provide different explicitly labeled document lists; checklist withheld."
        )
    page["evidence_coverage"] = coverage
    if not hits:
        record.update(result="no_hits", ms=int((time.perf_counter() - started) * 1000))
        return record
    print(f"      tier2: selected {page.get('source_url') or page['final_url']} "
          f"({len(page.markdown)} chars -> {len(source_md)} sent)", flush=True)

    # Search/fetch success alone is not enough to spend a parse call. These
    # lexical coverage signals only screen obviously incomplete packets; the
    # parser still has to validate each extracted claim and exact quote.
    coverage = page.get("evidence_coverage") or assess_evidence_coverage(
        source_md, item.canonical_name
    )
    record["evidence_coverage"] = coverage
    from services.tinyfish.field_evidence import build_field_evidence

    record["field_evidence"] = build_field_evidence(
        page.get("evidence_sources", []), item.canonical_name
    )
    _apply_document_evidence_status(coverage, record["field_evidence"])
    record["coverage_searches"] = page.get("coverage_searches", gap_searches)
    partial_documents = coverage.get("document_checklist", {}).get("status") in {
        "partial_rule_only", "needs_source_review",
    } or record.get("field_evidence", {}).get(
        "document_checklist", {}
    ).get("status") in {"partial_rule_only", "unknown"}
    if partial_documents:
        record["coverage_warnings"] = [
            "Official evidence supports an individual document/data rule, but not an exhaustive checklist."
        ]
    missing_coverage = [
        key for key in (
            "scheme_identity", "eligibility", "benefits", "document_checklist",
            "application_route", "continuation",
        )
        if not _coverage_status_is_useful(
            key, coverage.get(key, {}).get("status")
        )
    ]
    if missing_coverage:
        record.update(
            result="evidence_incomplete",
            missing_evidence=missing_coverage,
            source_documents=page.get("evidence_sources", []),
            evidence_packet=source_md,
            ms=int((time.perf_counter() - started) * 1000),
        )
        return record

    # This mode is used for auditable source-readiness checks. It deliberately
    # exits after TinyFish retrieval/coverage and can never call the LLM or DB.
    if coverage_only:
        record.update(
            result=("ready_for_parse_with_document_review"
                    if partial_documents else "ready_for_parse"),
            source_documents=page.get("evidence_sources", []),
            evidence_packet=source_md,
            ms=int((time.perf_counter() - started) * 1000),
        )
        return record

    # --- Ladder + storage ------------------------------------------------
    indexer = None
    if not dry_run:
        indexer = make_indexer(embedder, qdrant_client, collection)

    outcome = await ingest_markdown(
        source_md,
        fetch={"source_url": page.get("source_url") or page["final_url"],
               "final_url": page["final_url"], "content_hash": page.content_hash,
               "seed_query": item.query,
               "candidate_name": item.canonical_name,
               "evidence_coverage": coverage,
               "field_evidence": record.get("field_evidence", {}),
               "source_documents": page.get("evidence_sources", [])},
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
    if not args.coverage_only:
        from services.api.embedding import Embedder
        from services.tinyfish.parser import llm_extract

    seeds = load_seeds(args.seeds)
    if args.limit is not None:
        seeds = seeds[: max(args.limit, 0)]
    if not seeds:
        print("nothing to process", file=sys.stderr)
        return 1

    settings = get_settings()
    if args.coverage_only:
        db = qdrant_client = None
        before = {"mongo_total": 0, "mongo_indexed": 0,
                  "mongo_pending": 0, "qdrant_points": 0}
    else:
        _, db, qdrant_client = _connect(settings)
        before = db_counts(db, qdrant_client, settings.qdrant_collection)
    collection = settings.qdrant_collection
    mode = (
        "COVERAGE ONLY (TinyFish fetch; no LLM or DB)" if args.coverage_only
        else "DRY RUN (no writes)" if args.dry_run
        else "LIVE (writes enabled)"
    )
    print(f"batch ingest — {len(seeds)} seed(s) — {mode}")
    print(f"  mongo before: {before['mongo_total']} docs "
          f"({before['mongo_indexed']} indexed / {before['mongo_pending']} pending) | "
          f"qdrant before: {before['qdrant_points']} points\n")

    # The 2.3 GB embedder is only worth loading when something will be written.
    embedder = None if args.dry_run or args.coverage_only else Embedder()

    llm = (None if args.coverage_only else
           ThrottledLLM(llm_extract, gap_s=LLM_MIN_GAP_S, retries=LLM_RETRIES))
    tally: dict[str, int] = {}
    records: list[dict[str, Any]] = []
    wall_start = time.perf_counter()

    for i, item in enumerate(seeds):
        print(f"[{i + 1}/{len(seeds)}] {item.persona}: {item.query}", flush=True)
        try:
            rec = await process_seed(
                item, db=db, qdrant_client=qdrant_client, collection=collection,
                embedder=embedder, llm=llm, dry_run=args.dry_run,
                coverage_only=args.coverage_only,
                full_field_search=args.full_field_search,
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
            "evidence_incomplete": "[REVIEW]",
            "ready_for_parse": "[READY]",
            "ready_for_parse_with_document_review": "[READY/REVIEW]",
            "candidate_held_by_source_review": "[REVIEW]",
            "error": "[FAIL]",
        }.get(rec["result"], "[?]")
        detail = rec.get("error") or rec.get("name") or rec.get("scheme_id") or ""
        if rec.get("violations"):
            detail = f"{detail} {rec['violations'][:2]}"
        print(f"    {tag} {rec['result']}  {rec['ms']} ms  {detail}", flush=True)

    elapsed = time.perf_counter() - wall_start
    after = (before if args.coverage_only else db_counts(db, qdrant_client, collection))

    if args.coverage_report:
        report_path = args.coverage_report
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "coverage_only",
            "llm_calls": 0,
            "database_writes": 0,
            "records": records,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"  coverage report   {report_path}")

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
    print(f"  llm calls          {llm.calls if llm else 0}  "
          f"(rate-limit backoffs: {llm.backoffs if llm else 0})")
    print(f"  elapsed            {elapsed:.1f} s")
    if args.dry_run or args.coverage_only:
        print("  mode               dry run — nothing written")
    print("=" * 70)
    return 0


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.batch_ingest",
        description="Tier 1 → Tier 2 → configured LLM ladder → MongoDB/Qdrant (no Tier 3).",
    )
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="process only the first N seeds")
    parser.add_argument("--dry-run", action="store_true",
                        help="run discovery/fetch/ladder but write nothing")
    parser.add_argument("--coverage-only", action="store_true",
                        help="run TinyFish discovery/fetch and coverage only; no LLM or database connection")
    parser.add_argument("--full-field-search", action="store_true",
                        help="in coverage-only mode, run one serial TinyFish discovery pass for every scheme field")
    parser.add_argument("--coverage-report", type=pathlib.Path, default=None,
                        help="write detailed source and field evidence to a JSON file")
    parser.add_argument("--seeds", type=pathlib.Path, default=DEFAULT_SEEDS_PATH,
                        help=f"seed manifest (default: {DEFAULT_SEEDS_PATH})")
    args = parser.parse_args(argv)
    if args.full_field_search and not args.coverage_only:
        print("ERROR: --full-field-search requires --coverage-only", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run_async(args))
    except BatchIngestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
