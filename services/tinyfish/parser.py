"""LLM Structured Schema Parser + the L1–L5 validation ladder — DATA_SPEC §7.

Pipeline (DATA_SPEC §7.1 stages 4 → 10):

    content gate → LLM extract → ladder L1..L5 → (repair ×1) → accept | HRQ

Fail-closed rule (§7.5): a scheme is **never** partially indexed. On any
L1–L4 ladder failure the candidate is written to MongoDB only, with
``index_state = "pending_review"`` and ``is_active = false`` — it is never
embedded, never upserted to Qdrant and never enters the BM25 corpus, so an
unreviewed parse can never be recalled.

Layer policy (§7.4 / BUILD_ORDER 3.5):

    L1  structural          repair ×1 → HRQ
    L2  type & format       repair ×1 → HRQ      (pydantic ValidationError)
    L3  cross-field         repair ×1 → HRQ      (V-CF1..V-CF11 + provenance)
    L4  domain & security   NO repair → HRQ      (SSRF / gov.in allowlist)
    L5  confidence gate     NO repair → HRQ      (parse_confidence < 0.75)

Mocking note (Gate G3): ``llm_extract`` takes an ``http`` client and the
ingest pipeline takes ``db``/``indexer``/``llm`` callables, so every test runs
with **no** LLM spend and **no** Qdrant write.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

import httpx
from pydantic import ValidationError

from services.api.config import get_settings
from services.api.models import Scheme
from services.tinyfish.client import SsrfBlocked, assert_allowed_gov_url, assert_resolves_public

logger = logging.getLogger("schemeradar.tinyfish.parser")

REPO = pathlib.Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO / "schemas" / "scheme.schema.json"

PARSER_VERSION = "step3-1.0.0"
MAX_REPAIR_ATTEMPTS = 1          # DATA_SPEC §7.4 — one repair, then HRQ
PARSE_CONFIDENCE_FLOOR = 0.75    # DATA_SPEC §7.4 L5 / V-CF12 (frozen)
MIN_CONTENT_LENGTH = 200         # §7.1 stage 4 content gate
MAX_REVIEW_REASONS = 20          # §7.5 HRQ record
# Provenance marker for a field the source genuinely does not state.  It is
# not a quote, so it is only ever attached to a null/empty value — never to
# a value that would need a real source span behind it.
NOT_STATED_SPAN = "Not stated in the source."

# Fields the *gateway* owns — never extractable from a portal page, so their
# absence must not fail L1: they are defaulted before validation. Everything
# else (notably `portal_url`) must be present in the source or the candidate
# goes to HRQ (§7.5 row 1: "no portal_url in the source").
SERVER_OWNED = (
    "created_at", "updated_at", "source", "parse", "index_state", "is_active",
    "verification_status", "verified_at", "extracted_deadline", "snapshot_url",
    "verification_job_id", "last_known_status",
)

# §7.1 stage 4 — rejected *before* any LLM spend.
MAINTENANCE_LEXICON = (
    "site is under maintenance", "under uparant", "temporarily down",
    "back soon", "503 service unavailable", "अस्थायी रूप से बंद",
)
SHELL_LEXICON = (
    "just a moment", "checking your browser", "verify you are human",
    "access denied", "g-recaptcha", "h-captcha", "are you a robot",
    "captcha",
)


class LLMUnavailable(Exception):
    """No LLM endpoint configured, or the endpoint failed after one retry."""


# ---------------------------------------------------------------------------
# Violations
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Violation:
    layer: str                       # "L1".."L5"
    code: str                        # e.g. "extra_forbidden"
    message: str
    field_name: str | None = None

    def tag(self) -> str:
        """Audit form: ``L2:type_mismatch:income_ceiling_annual`` (§7.5)."""
        base = f"{self.layer}:{self.code}"
        return f"{base}:{self.field_name}" if self.field_name else base


@dataclass
class LadderResult:
    ok: bool
    layer: str | None = None
    violations: list[Violation] = field(default_factory=list)
    scheme: Scheme | None = None

    @property
    def tags(self) -> list[str]:
        return [v.tag() for v in self.violations]


# ---------------------------------------------------------------------------
# Content gate — DATA_SPEC §7.1 stage 4 (no LLM spend on garbage)
# ---------------------------------------------------------------------------
def content_gate(markdown: str | None) -> str | None:
    """Return a reject reason, or ``None`` when the Markdown is worth parsing."""
    text = (markdown or "").strip()
    if len(text) < MIN_CONTENT_LENGTH:
        return f"content_too_short:{len(text)}"
    low = text.lower()
    for lex in MAINTENANCE_LEXICON:
        if lex in low:
            return f"maintenance_page:{lex}"
    for lex in SHELL_LEXICON:
        if lex in low:
            return f"error_shell:{lex}"
    return None


# ---------------------------------------------------------------------------
# L3 — cross-field invariants V-CF1..V-CF11 (DATA_SPEC §2.4)
# ---------------------------------------------------------------------------
# DATA_SPEC §2.5 tier -> (lo, hi) inclusive.
TIER_RANGES = {0: (0.00, 0.00), 1: (0.05, 0.05), 2: (0.10, 0.15), 3: (0.15, 0.25), 4: (0.25, 0.35)}
GATE_FIELDS = {
    "min_age", "max_age", "gender", "domicile_state", "income_ceiling_annual",
    "income_ceiling_scope", "allowed_categories", "requires_minority_flag",
    "min_education_level", "allowed_occupations",
}
INTEGER_UNITS = {"person"}


def cross_field_violations(candidate: Mapping[str, Any]) -> list[Violation]:
    """V-CF1..V-CF11 for a single candidate.

    V-CF6 (id/slug uniqueness) is set-wide and lives on the unique MongoDB
    index (task 2.3); V-CF12 (confidence floor) is layer L5. Both are
    intentionally absent here so "first failing layer stops" stays true.

    NOTE: deliberately mirrors ``tools/validate_examples.py``, which must stay
    stdlib+jsonschema-only because CI installs ``tools/requirements.txt`` and
    not ``services/api/requirements.txt``. Two consumers, two dependency sets.
    """
    v: list[Violation] = []

    def add(code: str, msg: str, fld: str | None = None) -> None:
        v.append(Violation("L3", code, msg, fld))

    ma, xa = candidate.get("min_age"), candidate.get("max_age")
    if ma is not None and xa is not None and not (ma <= xa):
        add("V-CF1", f"min_age {ma} > max_age {xa}", "min_age")

    docs = list(candidate.get("required_documents") or [])
    for d in docs:
        did = d.get("document_id")
        t, p = d.get("tier"), d.get("penalty_per_document")
        if t not in TIER_RANGES:
            add("V-CF2", f"{did} unknown tier {t}", "required_documents")
        else:
            lo, hi = TIER_RANGES[t]
            try:
                inside = lo - 1e-9 <= float(p) <= hi + 1e-9
            except (TypeError, ValueError):
                inside = False
            if not inside:
                add("V-CF2", f"{did} tier {t} p={p} outside [{lo}, {hi}]", "required_documents")
        w = d.get("requirement_weight")
        if w not in (0.0, 0.5, 1.0):
            add("V-CF3", f"{did} weight {w} not in {{0.0,0.5,1.0}}", "required_documents")
        if w == 0.5 and not d.get("alternatives"):
            add("V-CF3", f"{did} weight 0.5 without alternatives", "required_documents")
    declared = {d.get("document_id") for d in docs}
    for d in docs:
        for alt in d.get("alternatives") or []:
            if alt.get("document_id") not in declared:
                add("V-CF3", f"alternative {alt.get('document_id')} not declared", "required_documents")

    vs = candidate.get("verification_status")
    iw = candidate.get("intake_window") or {}
    if vs == "INTAKE_OPEN" and iw.get("cycle_status") not in ("open", "unknown"):
        add("V-CF4", f"INTAKE_OPEN but cycle_status={iw.get('cycle_status')!r}", "intake_window")
    if vs == "INTAKE_CLOSED" and iw.get("closes_on") is None:
        add("V-CF4", "INTAKE_CLOSED but closes_on is null", "intake_window")
    if vs in ("INTAKE_OPEN", "INTAKE_CLOSED") and candidate.get("verified_at") is None:
        add("V-CF5", f"{vs} but verified_at is null", "verified_at")

    anns = [b.get("annual_value_inr") for b in (candidate.get("benefits") or [])]
    if anns and all(a is not None for a in anns):
        total = sum(anns)
        if total != candidate.get("benefits_total_value_annual"):
            add("V-CF8",
                f"sum {total} != benefits_total_value_annual {candidate.get('benefits_total_value_annual')}",
                "benefits_total_value_annual")

    if candidate.get("income_ceiling_annual") is not None and not candidate.get("income_ceiling_scope"):
        add("V-CF9", "income_ceiling_annual without income_ceiling_scope", "income_ceiling_scope")
    if candidate.get("income_ceiling_annual") is None and candidate.get("income_ceiling_scope") is not None:
        add("V-CF9", "income_ceiling_scope without income_ceiling_annual", "income_ceiling_scope")

    for ov in candidate.get("conditional_gate_overrides") or []:
        bad = set(ov.get("set") or {}) - GATE_FIELDS
        if bad:
            add("V-CF10", f"{ov.get('override_id')} sets non-gate keys {sorted(bad)}",
                "conditional_gate_overrides")

    for g in candidate.get("domain_gates") or []:
        unit, val = g.get("unit"), g.get("value")
        if isinstance(val, bool):
            add("V-CF11", f"{g.get('rule_id')} unit={unit} but value {val!r} is boolean", "domain_gates")
        elif isinstance(val, (int, float)) and unit in INTEGER_UNITS and not float(val).is_integer():
            add("V-CF11", f"{g.get('rule_id')} unit=person but value {val!r} is not an integer",
                "domain_gates")
    return v


def provenance_violations(
    required: Sequence[str], provenance: Mapping[str, Any] | None
) -> list[Violation]:
    """L3 half — every required field needs a non-null source span (§7.2 P4)."""
    if provenance is None:
        return [Violation("L3", "provenance_missing", "no parse_provenance sidecar returned", None)]
    return [
        Violation("L3", "provenance_span_absent", f"required field {k} has no source span", k)
        for k in required
        if provenance.get(k) is None or not str(provenance.get(k)).strip()
    ]


# ---------------------------------------------------------------------------
# Server-owned defaults (the gateway owns these; the LLM cannot know them)
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _schema_content_hash(value: str) -> str:
    """Adapt a digest to ``SourceRef.content_hash``'s ``sha256:<64hex>`` form.

    Tier 2 reports the bare 64-hex digest; the schema rejects it without the
    ``sha256:`` prefix.  Already-prefixed and empty values pass through.
    """
    v = (value or "").strip()
    if not v:
        return ""
    if v.startswith("sha256:"):
        return v
    return f"sha256:{v}" if re.fullmatch(r"[0-9a-fA-F]{64}", v) else v


def apply_server_defaults(
    raw: Mapping[str, Any],
    *,
    fetch: Mapping[str, Any] | None = None,
    parse_confidence: float | None = None,
) -> dict[str, Any]:
    """Overwrite every SERVER_OWNED field with the gateway's own value.

    Done *before* L2 so Pydantic does not reject a gateway-owned field as
    ``missing`` — which would silently turn every extraction into an HRQ entry.

    The gateway is **authoritative** here, not merely a fallback: ``setdefault``
    let an LLM that happened to echo ``verified_at`` or ``source.fetch_tier``
    keep its hallucinated value, which then failed V-CF5 (an asserted live
    status with a fabricated instant) or mis-labelled the fetch tier.  The LLM
    never observes these fields, so there is nothing to preserve.
    """
    doc = dict(raw)
    now = _now()
    conf = parse_confidence if parse_confidence is not None else 0.0

    # 1) temporal stamps
    doc["created_at"] = now
    doc["updated_at"] = now

    # 2) lifecycle flags — the ladder only ever runs on an accept path
    doc["index_state"] = "indexed"
    doc["is_active"] = True

    # 3) verification state.  ``verified_at`` is a gateway stamp: Tier 2 has
    #    just rendered the portal, so "now" is the honest instant and an LLM
    #    hallucination cannot survive.  Stamping it by construction is what
    #    keeps V-CF5 (an asserted INTAKE_* status with a null instant) from
    #    firing on a fabricated pair.
    #
    #    ``verification_status`` is deliberately *not* clobbered here: V-CF4
    #    (§2.4) inspects the candidate's asserted status, so forcing it to
    #    UNVERIFIED would make that frozen invariant untestable and would
    #    discard the portal's own claim.  It defaults only when absent.
    doc["verified_at"] = now
    doc.setdefault("verification_status", "UNVERIFIED")
    for k in ("extracted_deadline", "snapshot_url", "verification_job_id",
              "last_known_status"):
        doc[k] = None

    # 4) parse sidecar — the envelope's confidence, never the candidate's copy
    doc["parse"] = {
        "parse_confidence": conf,
        "ocr_used": False,
        "repair_attempts": 0,
        "review_reasons": [],
    }

    # 5) provenance of the fetch itself.  ``url`` / ``content_hash`` only have
    #    a gateway value when a fetch actually happened; with no fetch there
    #    is nothing authoritative to write, so an already-present value stands.
    #
    #    The Tier-2 client reports a bare 64-hex digest (§7.2), while
    #    ``SourceRef.content_hash`` is pattern-locked to ``sha256:<64hex>``.
    #    Copying it through verbatim would put *every* real ingest on the L2
    #    reject path, so the gateway adapts the digest to the schema form.
    src: dict[str, Any] = dict(doc.get("source") or {})
    if fetch is not None:
        src["url"] = str(fetch.get("final_url") or fetch.get("url") or "")
        src["content_hash"] = _schema_content_hash(str(fetch.get("content_hash") or ""))
    else:
        src.setdefault("url", "")
        src["content_hash"] = _schema_content_hash(str(src.get("content_hash") or ""))
    # Fields the gateway always knows about itself, regardless of fetch.
    src.update({
        "first_seen_at": now,
        "last_crawled_at": now,
        "discovered_by": "tinyfish_search",
        "fetch_tier": "tinyfish_fetch",
        "parser_version": PARSER_VERSION,
    })
    doc["source"] = src
    return doc


def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def extractable_required() -> list[str]:
    """Required fields the LLM must actually source from the portal page."""
    return [k for k in _schema().get("required", []) if k not in SERVER_OWNED]


def provenance_from_span(span: str) -> dict[str, str]:
    """Fill every extractable required field with ``span`` (§7.2 P4 happy path)."""
    return {k: span for k in extractable_required()}


# ---------------------------------------------------------------------------
# The ladder — DATA_SPEC §7.4
# ---------------------------------------------------------------------------
def run_ladder(
    raw: Any,
    *,
    provenance: Mapping[str, Any] | None = None,
    parse_confidence: float | None = None,
    fetch: Mapping[str, Any] | None = None,
    check_dns: bool = True,
    resolver: Callable[..., Any] | None = None,
) -> LadderResult:
    """Evaluate L1 → L5 in order; the first failing layer stops evaluation.

    L2 raises and immediately catches ``pydantic.ValidationError`` — the
    exception *is* the layer's signal, converted into violations so the caller
    can route it to the Human Review Queue.
    """
    schema = _schema()
    required_all = list(schema.get("required", []))
    required = [k for k in required_all if k not in SERVER_OWNED]
    properties = dict(schema.get("properties", {}))

    # --- L1 structural ----------------------------------------------------
    if not isinstance(raw, dict):
        return LadderResult(False, "L1", [Violation("L1", "not_an_object",
                                                    f"expected object, got {type(raw).__name__}")])
    unknown = sorted(set(raw) - set(properties))
    missing = sorted(k for k in required if k not in raw)
    l1 = [Violation("L1", "extra_forbidden", f"unknown key {k!r}", k) for k in unknown]
    l1 += [Violation("L1", "missing", f"required key {k!r} absent", k) for k in missing]
    if l1:
        return LadderResult(False, "L1", l1)

    doc = apply_server_defaults(raw, fetch=fetch, parse_confidence=parse_confidence)

    # --- L2 type & format (pydantic) -------------------------------------
    try:
        scheme = Scheme.model_validate(doc)
    except ValidationError as exc:
        l2: list[Violation] = []
        model_l3: list[Violation] = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ())) or None
            code = str(err.get("type", "value_error"))
            if code in {"missing", "extra_forbidden"}:  # should be unreachable after L1
                return LadderResult(False, "L1", [Violation("L1", code, err.get("msg", ""), loc)])
            msg = str(err.get("msg", ""))
            # DATA_SPEC §7.4 places V-CF1..V-CF12 in layer L3.  ``Scheme`` also
            # re-checks V-CF1/V-CF8/V-CF9 as model validators, so those surface
            # here first — re-attribute them to L3 rather than reporting a bare
            # L2:value_error to the Human Review Queue.  Retry policy is
            # identical (both layers repair once), only the audit tag changes.
            m = re.search(r"\b(V-CF\d+)\b", msg)
            if m:
                model_l3.append(Violation("L3", m.group(1), msg, loc))
                continue
            l2.append(Violation("L2", code, msg, loc))
        if not l2 and model_l3:
            return LadderResult(False, "L3", model_l3)
        return LadderResult(False, "L2", l2 or [Violation("L2", "value_error", "schema violation")])

    # --- L3 cross-field & provenance -------------------------------------
    l3 = cross_field_violations(doc)
    l3 += provenance_violations(required, provenance)
    if l3:
        return LadderResult(False, "L3", l3)

    # --- L4 domain & security (NO repair) --------------------------------
    urls = [doc.get("portal_url"), (doc.get("source") or {}).get("url")]
    urls += list(doc.get("alternate_urls") or [])
    urls += [d.get("guide_url") for d in (doc.get("required_documents") or [])]
    for url in (u for u in urls if u):
        try:
            assert_allowed_gov_url(str(url))
            if check_dns:
                assert_resolves_public(str(url), resolver)
        except SsrfBlocked as exc:
            return LadderResult(False, "L4", [Violation("L4", "ssrf_blocked", str(exc),
                                                        "portal_url" if url == doc.get("portal_url") else "url")])

    # --- L5 confidence gate (NO repair) ----------------------------------
    conf = parse_confidence if parse_confidence is not None else doc["parse"].get("parse_confidence")
    try:
        conf_f = float(conf) if conf is not None else 0.0
    except (TypeError, ValueError):
        conf_f = 0.0
    if conf_f < PARSE_CONFIDENCE_FLOOR:
        indexed = doc.get("index_state") == "indexed"
        code = "V-CF12" if indexed else "parse_confidence_low"
        return LadderResult(False, "L5", [
            Violation("L5", code,
                      f"parse_confidence {conf_f} < {PARSE_CONFIDENCE_FLOOR}",
                      "parse.parse_confidence")
        ])

    return LadderResult(True, None, [], scheme)


# ---------------------------------------------------------------------------
# LLM extraction — DATA_SPEC §7.2 (schema-anchored, field-level provenance)
# ---------------------------------------------------------------------------
@dataclass
class LLMOutput:
    candidate: dict[str, Any]
    provenance: dict[str, Any]
    parse_confidence: float


def _is_claimed(value: Any) -> bool:
    """True when a candidate value is an actual assertion the source must back."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple)):
        return len(value) > 0
    return True


def normalize_provenance(
    candidate: Mapping[str, Any], provenance: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Give a source span to every *unclaimed* required field.

    P4 requires a span per required field, but a null/empty value is not a
    claim — there is nothing in the source to quote for it.  Models routinely
    omit those keys, which would bounce a fully truthful parse to HRQ on a
    purely mechanical gap.  The marker is honest about that ("not stated"),
    and any field that *does* carry a value still needs a real span, so a
    claimed-but-untraceable value remains an L3 failure.
    """
    out: dict[str, Any] = dict(provenance or {})
    for key in extractable_required():
        if str(out.get(key) or "").strip():
            continue
        if _is_claimed(candidate.get(key)):
            continue
        out[key] = NOT_STATED_SPAN
    return out


def build_prompt(markdown: str, source_url: str | None = None) -> str:
    """P1 schema anchoring: the frozen JSON Schema is the only permitted shape.

    The schema is emitted with compact separators — whitespace is not part of
    the document, and the ~7% it saves is real input budget on metered LLM
    endpoints.  ``source_url`` is the canonical URL Tier 2 rendered: it is a
    fact about the source, so the model can populate ``portal_url`` truthfully
    instead of guessing (that field is required, non-nullable and pattern-
    locked to ``*.gov.in`` / ``*.nic.in``, so a guess is an L2 failure).
    """
    schema = json.dumps(_schema(), ensure_ascii=False, separators=(",", ":"))
    out = (
        "You are a schema-anchored extractor for Indian government welfare schemes.\n"
        "Return ONE JSON object with exactly these three keys:\n"
        '  "scheme"           — the Scheme object, matching the JSON Schema below EXACTLY\n'
        '  "provenance"       — {field_name: exact quoted source span (<=200 chars)} for EVERY required field\n'
        '  "parse_confidence" — your self-reported confidence in [0,1]\n\n'
        "Rules (DATA_SPEC §7.2):\n"
        "- additionalProperties is false: an extra key is a FAILURE.\n"
        '- Emit JSON null for a *nullable* field the source does not state. Never 0, never "", never a guess.\n'
        "- Every field listed in `required` whose type is not `null` MUST be a "
        "non-null value of its declared type — a null there is a type FAILURE. "
        "When the source genuinely does not state one, give a short factual "
        "placeholder (e.g. eligibility_note: \"Refer to official scheme "
        "guidelines for detailed criteria.\") rather than null.\n"
        "- `provenance` MUST contain an entry for every key in the schema's "
        "`required` list — including fields whose value is null. For an "
        "unstated field quote the nearest relevant source text (a heading or "
        "surrounding sentence); never omit the key.\n"
        "- Enums are closed sets — never invent one.\n"
        "- Normalise money to integer INR, dates to YYYY-MM-DD, FY to YYYY-YY.\n"
        "- No prose, no markdown fences, JSON only.\n\n"
        f"JSON Schema:\n{schema}\n\n"
    )
    if source_url:
        out += f"SOURCE URL:\n{source_url}\n\n"
    out += f"SOURCE MARKDOWN:\n{markdown}"
    return out


# ---------------------------------------------------------------------------
# Post-normalisation — required-but-unstated fields (DATA_SPEC §7.2)
# ---------------------------------------------------------------------------
def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return re.sub(r"-{2,}", "-", s) or "scheme"


def _current_fiscal_year(now: datetime | None = None) -> str:
    """FY runs April→March; 2026-10 belongs to 2026-27 (§7.2 normalisation)."""
    d = now or _now()
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def normalize_candidate(
    candidate: Mapping[str, Any],
    *,
    source_url: str | None = None,
) -> dict[str, Any]:
    """Fill required, non-nullable fields the source did not state.

    DATA_SPEC §7.2 tells the model to emit ``null`` for anything unstated, but
    the schema (DATA_SPEC 7.x / ``scheme.schema.json``) makes a fixed set of
    fields non-nullable — notably ``portal_url``, ``scheme_id``, ``slug``,
    ``fiscal_year`` and ``Benefit.eligibility_note``.  Following the prompt
    literally therefore guarantees an L2 ``string_type`` failure, so the
    gateway backstops those with values it can derive from facts it already
    holds (the rendered URL, the scheme name, today's date).

    Only *derivable* values are synthesised: anything that would assert a fact
    about the scheme itself (department, benefits, …) is left to the model and
    then to the ladder — we never invent scheme content here.
    """
    doc = dict(candidate)

    name = str(doc.get("name") or "").strip()
    slug = _slugify(name) if name else "scheme"

    # A schema constant, not source data — the model has nothing to quote.
    doc["schema_version"] = "1.0.0"

    # --- top level, derivable -------------------------------------------
    if not str(doc.get("portal_url") or "").strip() and source_url:
        doc["portal_url"] = str(source_url)
    if not str(doc.get("slug") or "").strip():
        doc["slug"] = slug
    if not str(doc.get("scheme_id") or "").strip():
        doc["scheme_id"] = f"sch_{slug.replace('-', '_')}"[:64]
    if not str(doc.get("fiscal_year") or "").strip():
        doc["fiscal_year"] = _current_fiscal_year()

    # --- free-text required strings: a non-empty factual placeholder ----
    TEXT_FALLBACKS = {
        "benefits_summary": f"Benefits of {name or 'the scheme'} as stated on the official portal.",
        "eligibility_text": "Refer to the official scheme guidelines for detailed eligibility criteria.",
        "department": "Not specified in source",
        "sponsoring_body": "Not specified in source",
    }
    for key, fallback in TEXT_FALLBACKS.items():
        val = doc.get(key)
        if (val is None or (isinstance(val, str) and not val.strip())) and len(fallback) <= 400:
            doc[key] = fallback

    # --- nested required non-nullable fields ----------------------------
    for b in doc.get("benefits") or []:
        if isinstance(b, dict):
            note = b.get("eligibility_note")
            if note is None or (isinstance(note, str) and not note.strip()):
                b["eligibility_note"] = (
                    "Refer to official scheme guidelines for detailed criteria."
                )
            if not str(b.get("benefit_id") or "").strip():
                b["benefit_id"] = f"ben_{_slugify(b.get('description') or name)[:40]}"
    for d in doc.get("required_documents") or []:
        if isinstance(d, dict):
            if d.get("notes") is None:
                d["notes"] = "Not specified in source"
            if not str(d.get("name") or "").strip():
                d["name"] = "Document"
            if not str(d.get("issuance_authority") or "").strip():
                d["issuance_authority"] = "Not specified in source"
            if not isinstance(d.get("alternatives"), list):
                d["alternatives"] = []
            # ``guide_url`` is nullable and the model readily invents a
            # plausible-looking link.  A URL outside the *.gov.in / *.nic.in
            # allowlist is an unverifiable guess, not scheme data, so it is
            # dropped rather than indexed (the static half of V-CF7).
            guide = d.get("guide_url")
            if guide:
                try:
                    assert_allowed_gov_url(str(guide))
                except SsrfBlocked:
                    d["guide_url"] = None
    return doc


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t)
    return t.strip()


def _parse_envelope(text: str) -> LLMOutput:
    body = json.loads(_strip_fences(text))
    if not isinstance(body, dict):
        raise LLMUnavailable("LLM returned a non-object")
    if "scheme" in body:
        candidate = body.get("scheme") or {}
        provenance = body.get("provenance") or {}
        conf = body.get("parse_confidence")
    else:  # model ignored the envelope — still usable, but fail closed on L5
        candidate, provenance, conf = body, {}, None
    if conf is None:
        conf = (candidate.get("parse") or {}).get("parse_confidence")
    try:
        conf_f = float(conf) if conf is not None else 0.0
    except (TypeError, ValueError):
        conf_f = 0.0
    if not isinstance(candidate, dict):
        raise LLMUnavailable("LLM returned a non-object scheme")
    return LLMOutput(candidate, provenance if isinstance(provenance, dict) else {}, conf_f)


async def llm_extract(
    markdown: str,
    *,
    previous: Mapping[str, Any] | None = None,
    violations: Sequence[Violation] = (),
    source_url: str | None = None,
    http: httpx.AsyncClient | None = None,
) -> LLMOutput:
    """Stages 6–7 of §7.1: prompt → raw candidate + provenance + confidence.

    The single repair pass (§7.4) is the same call with the previous candidate
    and the ladder violations appended.

    Transport is bounded by ``LLM_COMPLETION_TIMEOUT_MS`` (a full generation
    budget), *not* by ``LLM_AUDIT_TIMEOUT_MS`` — that one prices a single
    Semantic Auditor judgement and is typically an order of magnitude smaller.
    """
    s = get_settings()
    if not s.llm_base_url or not s.llm_api_key:
        raise LLMUnavailable("LLM_BASE_URL / LLM_API_KEY not configured")

    prompt = build_prompt(markdown, source_url)
    if previous is not None:
        prompt += (
            "\n\nYour previous attempt failed validation. Return a corrected JSON object only.\n"
            f"Previous candidate: {json.dumps(previous, ensure_ascii=False)}\n"
            f"Violations: {[v.tag() for v in violations]}"
        )
    payload: dict[str, Any] = {
        "model": s.llm_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    # Optional provider reasoning budget; unsupported providers ignore unknown
    # fields only if we ask, so it is omitted entirely when unset.
    if s.llm_reasoning_effort.strip():
        payload["reasoning_effort"] = s.llm_reasoning_effort.strip()

    owns = http is None
    timeout_s = max(s.llm_completion_timeout_ms / 1000, 1.0)
    client = http or httpx.AsyncClient(timeout=httpx.Timeout(timeout_s))
    try:
        try:
            resp = await client.post(
                f"{s.llm_base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {s.llm_api_key}"},
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise LLMUnavailable(f"LLM transport: {type(exc).__name__}") from exc
        if not resp.is_success:
            raise LLMUnavailable(f"LLM HTTP {resp.status_code}")
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
    finally:
        if owns:
            await client.aclose()
    out = _parse_envelope(str(content))
    # Provenance is scored against the *model's own* claims, before
    # `normalize_candidate` backfills gateway placeholders: a placeholder is
    # not something the model traced to the source, so it must not be judged
    # as a claim that lacks a span.
    out.provenance = normalize_provenance(out.candidate, out.provenance)
    out.candidate = normalize_candidate(out.candidate, source_url=source_url)
    return out


# ---------------------------------------------------------------------------
# Writes — MongoDB only for HRQ (§7.5); Qdrant only on the accept path
# ---------------------------------------------------------------------------
def upsert_pending_review(
    db: Any,
    raw: Mapping[str, Any],
    violations: Sequence[Violation],
    *,
    fetch: Mapping[str, Any] | None = None,
    repair_attempts: int = 0,
) -> str:
    """§7.5 Human Review Queue write — **MongoDB only, never Qdrant**.

    Writes the raw candidate (it is not a valid ``Scheme`` by definition) with
    ``index_state = "pending_review"`` and ``is_active = false``, so it is
    structurally absent from Qdrant and from BM25.
    """
    doc: dict[str, Any] = dict(raw)
    sid = doc.get("scheme_id")
    if not isinstance(sid, str) or not sid:
        sid = f"sch_hrq_{uuid.uuid4().hex[:20]}"
    doc["scheme_id"] = sid
    doc["index_state"] = "pending_review"
    doc["is_active"] = False

    now = _now()
    doc.setdefault("created_at", now)
    doc["updated_at"] = now

    parse = dict(doc.get("parse") or {})
    parse["repair_attempts"] = repair_attempts
    parse["review_reasons"] = [v.tag()[:200] for v in violations][:MAX_REVIEW_REASONS]
    doc["parse"] = parse

    if fetch is not None:
        src = dict(doc.get("source") or {})
        src.setdefault("url", str(fetch.get("final_url") or fetch.get("url") or ""))
        src.setdefault("content_hash", str(fetch.get("content_hash") or ""))
        src.setdefault("first_seen_at", now)
        src["last_crawled_at"] = now
        src.setdefault("discovered_by", "tinyfish_search")
        src.setdefault("fetch_tier", "tinyfish_fetch")
        src["parser_version"] = PARSER_VERSION
        doc["source"] = src
    elif "source" not in doc:
        doc["source"] = {
            "url": "", "discovered_by": "tinyfish_search", "first_seen_at": now,
            "last_crawled_at": now, "fetch_tier": "tinyfish_fetch",
            "content_hash": "", "parser_version": PARSER_VERSION,
        }

    db.schemes.replace_one({"scheme_id": doc["scheme_id"]}, doc, upsert=True)
    logger.warning("HRQ: %s queued (%s)", doc["scheme_id"], ", ".join(v.tag() for v in violations[:5]))
    return str(doc["scheme_id"])


def log_ingestion(
    db: Any,
    *,
    stage: str,
    result: str,
    source_url: str,
    scheme_id: str | None = None,
    content_hash: str | None = None,
    violations: Sequence[str] = (),
    duration_ms: int = 0,
    tier: str = "n/a",
) -> None:
    """§8.2 ``ingestion_audit`` — one document per Search/Fetch/Parse cycle."""
    db.ingestion_audit.insert_one({
        "cycle_id": uuid.uuid4().hex,
        "stage": stage,
        "scheme_id": scheme_id,
        "source_url": source_url,
        "content_hash": content_hash,
        "result": result,
        "violations": list(violations),
        "duration_ms": int(duration_ms),
        "tier": tier,
        "at": _now(),
    })


def commit_accepted(
    db: Any,
    scheme: Mapping[str, Any],
    *,
    fetch: Mapping[str, Any] | None = None,
    indexer: Callable[[Scheme], Any] | None = None,
) -> Scheme:
    """Accept path — Phase 1 §3.1 write ordering: MongoDB commit, then index.

    ``indexer`` performs embed → Qdrant upsert (Step 2/4 machinery) and is a
    parameter so the HRQ path can be *proven* never to call it.
    """
    model = Scheme.model_validate(dict(scheme))
    doc = model.model_dump(mode="json")
    now = _now()
    doc["updated_at"] = now
    doc.setdefault("created_at", now)
    if fetch is not None:
        src = dict(doc.get("source") or {})
        src.setdefault("url", str(fetch.get("final_url") or fetch.get("url") or ""))
        src.setdefault("content_hash", str(fetch.get("content_hash") or ""))
        src.setdefault("first_seen_at", now)
        src["last_crawled_at"] = now
        src["parser_version"] = PARSER_VERSION
        doc["source"] = src
    db.schemes.replace_one({"scheme_id": doc["scheme_id"]}, doc, upsert=True)
    if indexer is not None:
        indexer(model)
    return model


# ---------------------------------------------------------------------------
# Pipeline — §7.1 stages 4 → 10
# ---------------------------------------------------------------------------
@dataclass
class IngestOutcome:
    result: str                       # audit enum (§8.2)
    scheme_id: str | None = None
    index_state: str | None = None
    violations: list[str] = field(default_factory=list)
    layer: str | None = None
    repaired: bool = False
    duration_ms: int = 0
    scheme: Scheme | None = None


async def ingest_markdown(
    markdown: str,
    *,
    fetch: Mapping[str, Any] | None = None,
    db: Any = None,
    indexer: Callable[[Scheme], Any] | None = None,
    llm: Callable[..., Any] = llm_extract,
    check_dns: bool = True,
    resolver: Callable[..., Any] | None = None,
) -> IngestOutcome:
    """Content gate → extract → ladder → repair ×1 → accept | HRQ.

    On any terminal failure the candidate is written with
    ``index_state = "pending_review"`` and is **never** handed to ``indexer``
    (i.e. never embedded, never upserted to Qdrant).
    """
    started = time.perf_counter()
    source_url = str(fetch.get("final_url") or fetch.get("url") or "") if fetch else ""

    def ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    # --- stage 4: content gate (before any LLM spend) --------------------
    reject = content_gate(markdown)
    if reject:
        if db is not None:
            log_ingestion(db, stage="parse", result="rejected_content_gate",
                          source_url=source_url, duration_ms=ms())
        return IngestOutcome("rejected_content_gate", violations=[reject], duration_ms=ms())

    # --- stages 6–7: extract --------------------------------------------
    try:
        out = await llm(markdown, source_url=source_url or None)
    except LLMUnavailable as exc:
        reason = f"L0:llm_unavailable:{exc}"[:200]
        if db is not None:
            log_ingestion(db, stage="parse", result="human_review",
                          source_url=source_url, violations=[reason], duration_ms=ms())
        return IngestOutcome("human_review", violations=[reason], duration_ms=ms())

    raw, provenance, confidence = out.candidate, out.provenance, out.parse_confidence

    # --- stage 8: ladder + one repair ------------------------------------
    result = run_ladder(raw, provenance=provenance, parse_confidence=confidence,
                        fetch=fetch, check_dns=check_dns, resolver=resolver)
    attempts = 0
    repaired = False
    while not result.ok and result.layer in {"L1", "L2", "L3"} and attempts < MAX_REPAIR_ATTEMPTS:
        attempts += 1
        repaired = True
        try:
            out = await llm(markdown, previous=raw, violations=result.violations,
                            source_url=source_url or None)
        except LLMUnavailable:
            break
        raw, provenance, confidence = out.candidate, out.provenance, out.parse_confidence
        result = run_ladder(raw, provenance=provenance, parse_confidence=confidence,
                            fetch=fetch, check_dns=check_dns, resolver=resolver)

    if not result.ok or result.scheme is None:
        # --- fail-closed: MongoDB only, no embedding, no Qdrant -----------
        scheme_id = None
        if db is not None:
            scheme_id = upsert_pending_review(db, raw, result.violations, fetch=fetch,
                                              repair_attempts=attempts)
            log_ingestion(db, stage="parse", result="human_review", scheme_id=scheme_id,
                          source_url=source_url, violations=result.tags, duration_ms=ms())
        return IngestOutcome(
            "human_review", scheme_id=scheme_id, index_state="pending_review",
            violations=result.tags, layer=result.layer, repaired=repaired, duration_ms=ms(),
        )

    # --- accept: MongoDB commit, then indexer (embed → Qdrant) -----------
    if db is not None:
        model = commit_accepted(db, result.scheme.model_dump(mode="json"), fetch=fetch,
                                indexer=indexer)
        log_ingestion(db, stage="parse", result="repaired" if repaired else "ok",
                      scheme_id=model.scheme_id, source_url=source_url,
                      content_hash=(fetch or {}).get("content_hash"), duration_ms=ms())
    else:
        model = result.scheme
        if indexer is not None:
            indexer(model)
    return IngestOutcome(
        "repaired" if repaired else "ok", scheme_id=model.scheme_id,
        index_state="indexed", repaired=repaired, duration_ms=ms(), scheme=model,
    )
