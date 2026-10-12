"""Field-focused evidence extraction from TinyFish-fetched official pages.

This module preserves exact passages and provenance. It does not normalize
them into eligibility or benefit facts; unsupported or ambiguous fields stay
unknown for a later parser/consumer.
"""

from __future__ import annotations

import re
from typing import Any, Sequence


_FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "scheme_identity": re.compile(r"\b(scheme|yojana|mission|programme|program)\b", re.I),
    "eligibility": re.compile(
        r"\b(eligib\w*|ineligible|who can|criteria|qualif\w*|beneficiar\w*|"
        r"must be|shall be|not eligible)\b", re.I,
    ),
    "benefits": re.compile(
        r"(?:₹|\b(?:rs\.?|inr)\s*)\s*[\d,]+|\b(subsid\w*|benefit|assistance|"
        r"scholarship|pension|grant|instal?ment|financial support)\b", re.I,
    ),
    "document_checklist": re.compile(
        r"\b(document\w*|aadhaar|land record|bank passbook|income certificate|"
        r"caste certificate|identity proof)\b", re.I,
    ),
    "application_route": re.compile(
        r"\b(apply|application|register|registration|enrol|enroll|portal|"
        r"common service centre|common service center|csc|office|patwari)\b", re.I,
    ),
    "continuation": re.compile(
        r"\b(continue\w*|continuation|extended|extension|valid up to|"
        r"approved for|scheme period|20\d{2}[-–]\d{2})\b", re.I,
    ),
}
_SENTENCE_SPLIT = re.compile(r"(?<!Rs\.)(?<=[.!?।])\s+|\n+", re.I)
_CHECKLIST_MARKER = re.compile(
    r"(?:following documents|documents required include|documents to be "
    r"(?:submitted|uploaded)|application .* accompanied by)", re.I,
)
_IDENTITY_STOP = {
    "pradhan", "mantri", "yojana", "scheme", "national", "india", "mission",
    "programme", "program", "central", "of", "for", "the", "and",
}


def build_field_evidence(
    source_documents: Sequence[dict[str, Any]],
    candidate_name: str | None,
    *,
    max_claims_per_field: int = 8,
) -> dict[str, dict[str, Any]]:
    """Return verbatim field passages with URL/hash/date provenance."""
    tokens = [
        token for token in re.findall(r"[a-z0-9]+", (candidate_name or "").lower())
        if len(token) >= 4 and token not in _IDENTITY_STOP
    ]
    output: dict[str, dict[str, Any]] = {}
    for field, pattern in _FIELD_PATTERNS.items():
        claims: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        per_source: dict[str, int] = {}
        for source in source_documents:
            url = str(source.get("url") or source.get("source_url") or "")
            body = str(source.get("text") or source.get("evidence_excerpt") or "")
            if not url or not body:
                continue
            for raw in _SENTENCE_SPLIT.split(body):
                quote = " ".join(raw.split())
                if not quote or not pattern.search(quote):
                    continue
                if field == "scheme_identity" and tokens and not any(
                    re.search(rf"\b{re.escape(token)}\b", quote, re.I) for token in tokens
                ):
                    continue
                if field == "continuation" and candidate_name and not any(
                    re.search(rf"\b{re.escape(token)}\b", quote, re.I) for token in tokens
                ):
                    continue
                marker = (url, quote)
                if marker in seen or per_source.get(url, 0) >= 2:
                    continue
                seen.add(marker)
                claims.append({
                    "quote": quote[:600],
                    "source_url": url,
                    "content_hash": source.get("content_hash"),
                    "published_date": source.get("published_date"),
                    "fetched_at": source.get("fetched_at"),
                    "retrieved_for": source.get("retrieved_for"),
                })
                per_source[url] = per_source.get(url, 0) + 1
                if len(claims) >= max_claims_per_field:
                    break
            if len(claims) >= max_claims_per_field:
                break
        if field == "document_checklist":
            if any(_CHECKLIST_MARKER.search(str(claim["quote"])) for claim in claims):
                status = "checklist_passage_found"
            elif claims:
                status = "partial_rule_only"
            else:
                status = "unknown"
        else:
            status = "passages_found" if claims else "unknown"
        output[field] = {
            "status": status,
            "claims": claims,
        }
    return output
