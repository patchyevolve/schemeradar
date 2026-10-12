"""Conservative, evidence-only conflict checks for TinyFish source packets.

These checks do not decide which source is right. They only identify claims
that are directly comparable from their wording and retain the evidence needed
for a later retry or audit. Ambiguous cases stay unknown.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Sequence


_SENTENCE_SPLIT = re.compile(r"(?<!Rs\.)(?<=[.!?।])\s+|\n+", re.I)
_AMOUNT = re.compile(
    r"(?:₹|\b(?:rs\.?|inr)\s*)([\d,]+(?:\.\d+)?)|"
    r"\b([\d,]+(?:\.\d+)?)\s*(percent|%)\b", re.I
)
_BENEFIT_CONTEXT = re.compile(
    r"\b(benefit|income support|financial assistance|assistance|subsid\w*|"
    r"premium|scholarship|pension|grant|indemnity|sum insured|insurance cover|"
    r"instal?ment|amount payable|amount of)\b", re.I,
)
_BUDGET_ONLY = re.compile(r"\b(total )?(financial )?outlay\b|\bbudget allocation\b", re.I)
_NOT_BENEFICIARY_VALUE = re.compile(
    r"\b(save|saves|saving|savings|electricity costs|government expenditure|"
    r"government cost|total outlay|budget allocation|crores?)\b", re.I,
)


def _amount_and_basis(sentence: str, match: re.Match[str]) -> tuple[str, str, str] | None:
    raw = match.group(1) or match.group(2)
    if not raw:
        return None
    amount = raw.replace(",", "")
    dimension = "percent" if match.group(3) else "currency"
    if dimension == "percent":
        amount = f"{amount}%"
    left = sentence[max(0, match.start() - 100):match.start()].lower()
    right = sentence[match.end():match.end() + 120].lower()
    context = f"{left} {right}"
    if _BUDGET_ONLY.search(sentence):
        return None

    # Compare only claims with a visible, shared payment/coverage basis.
    basis_terms = {
        "annual": r"\b(per year|per annum|annually|annual)\b",
        "monthly": r"\b(per month|monthly)\b",
        "instalment": r"\b(per instal?ment|instal?ments?)\b",
        "family": r"\b(per family|farmer famil\w*)\b",
        "member": r"\b(per member|per person)\b",
        "hectare": r"\b(per hectare|per ha)\b",
        "acre": r"\b(per acre)\b",
        "premium": r"\bpremium\b",
        "subsidy": r"\bsubsid\w*\b",
        "per_kw": r"\b(per k?w|per kilowatt)\b",
        "insurance": r"\b(sum insured|insurance cover|indemnity)\b",
        "scholarship": r"\bscholarship\b",
        "pension": r"\bpension\b",
        "percentage": r"\b(percent|percentage|%)\b",
        "one_time": r"\b(one[- ]time|lump sum)\b",
    }
    found = [name for name, pattern in basis_terms.items()
             if re.search(pattern, context, re.I)]
    basis = "+".join([*found, dimension])
    if not basis:
        return None
    period = next((f"{value}+{dimension}" for value in ("annual", "monthly", "instalment")
                   if value in found), "")
    return amount, basis, period


def _benefit_claims(source_documents: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    claims: list[dict[str, str]] = []
    for source in source_documents:
        url = str(source.get("url") or source.get("source_url") or "")
        body = str(source.get("text") or source.get("evidence_excerpt") or "")
        if not url or not body:
            continue
        for sentence in _SENTENCE_SPLIT.split(body):
            if (not _BENEFIT_CONTEXT.search(sentence)
                    or _NOT_BENEFICIARY_VALUE.search(sentence)):
                continue
            for match in _AMOUNT.finditer(sentence):
                normalized = _amount_and_basis(sentence, match)
                if normalized is None:
                    continue
                amount, basis, period = normalized
                claims.append({
                    "url": url,
                    "amount": amount,
                    "basis": basis,
                    "period": period,
                    "passage": sentence.strip()[:500],
                })
    return claims


def review_source_conflicts(
    source_documents: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Flag directly comparable numeric claims; never select a winning source.

    A conflict is reported only when two different URLs state different amounts
    with the same explicit basis (for example, different annual amounts). Many
    distinct benefit amounts without a shared basis are reported as
    ``multiple_unclassified_claims`` and remain unresolved rather than being
    incorrectly called a contradiction.
    """
    claims = _benefit_claims(source_documents)
    by_basis: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    by_period: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for claim in claims:
        by_basis[claim["basis"]][claim["amount"]].add(claim["url"])
        if claim["period"]:
            by_period[claim["period"]][claim["amount"]].add(claim["url"])

    conflicts = []
    corroborated = []
    for basis, amounts in by_basis.items():
        if len(amounts) > 1:
            per_source: dict[str, set[str]] = defaultdict(set)
            for claim in claims:
                if claim["basis"] == basis:
                    per_source[claim["url"]].add(claim["amount"])
            # If one source lists multiple values for the same measure, they
            # may be legitimate tiers or household categories. Do not compare
            # that bundle against one value from another page as a conflict.
            directly_comparable = all(len(values) == 1 for values in per_source.values())
            urls = set(per_source)
            if len(urls) > 1 and directly_comparable:
                conflicts.append({
                    "basis": basis,
                    "amounts": sorted(amounts),
                    "source_urls": sorted(urls),
                    "claims": [c for c in claims if c["basis"] == basis],
                })
        elif amounts:
            amount, urls = next(iter(amounts.items()))
            if len(urls) > 1:
                corroborated.append({"basis": basis, "amount": amount,
                                     "source_urls": sorted(urls)})

    all_values = {claim["amount"] for claim in claims}
    if not conflicts and not corroborated:
        for period, amounts in by_period.items():
            for amount, urls in amounts.items():
                if len(urls) > 1:
                    corroborated.append({"basis": period, "amount": amount,
                                         "source_urls": sorted(urls)})

    if conflicts:
        benefit_status = "conflicting"
    elif corroborated:
        benefit_status = "corroborated"
    elif len(all_values) > 1:
        benefit_status = "multiple_unclassified_claims"
    elif claims:
        benefit_status = "single_source_claim"
    else:
        benefit_status = "no_comparable_numeric_claim"

    # A checklist is complete only when the source itself labels it as such.
    # A mismatch between two explicitly complete lists is a conflict; a few
    # mentions of documents are always partial evidence.
    checklist_sources: dict[str, set[str]] = defaultdict(set)
    checklist_pattern = re.compile(
        r"(?:following|required|supporting) documents|documents required|"
        r"documents to be (?:submitted|uploaded)|application .* accompanied by",
        re.I,
    )
    for source in source_documents:
        url = str(source.get("url") or source.get("source_url") or "")
        body = str(source.get("text") or source.get("evidence_excerpt") or "")
        if not url or not body:
            continue
        for sentence in _SENTENCE_SPLIT.split(body):
            if not checklist_pattern.search(sentence):
                continue
            docs = sorted(set(re.findall(
                r"\b(aadhaar|identity proof|income certificate|caste certificate|"
                r"bank passbook|land record|land ownership record|ration card|"
                r"residence certificate|disability certificate)\b", sentence, re.I
            )))
            if docs:
                checklist_sources[url].update(item.lower() for item in docs)
    explicit_lists = {url: docs for url, docs in checklist_sources.items() if docs}
    distinct_lists = {tuple(sorted(docs)) for docs in explicit_lists.values()}
    documents_status = (
        "conflicting" if len(explicit_lists) > 1 and len(distinct_lists) > 1
        else "one_explicit_list" if explicit_lists
        else "partial_or_unknown"
    )

    return {
        "benefits": {
            "status": benefit_status,
            "conflicts": conflicts,
            "corroborated": corroborated,
            "claims": claims,
        },
        "documents": {
            "status": documents_status,
            "explicit_lists": {url: sorted(docs) for url, docs in explicit_lists.items()},
        },
        "decision_rule": "Only exact shared units are compared; unclear or differently scoped claims stay unresolved.",
    }
