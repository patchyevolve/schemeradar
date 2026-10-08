"""Canonical corpus text for a Scheme (ARCHITECTURE §5.3.1 / §5.3.2).

Two consumers share one builder so sparse and dense recall can never drift
apart:

* **Embedder input** (§5.3.1): ``name + " " + department + " " + benefits_text
  + " " + eligibility_text`` → 1024-d ``BAAI/bge-m3`` vector for Qdrant.
* **BM25 corpus** (§5.3.2): ``name + department + benefits + eligibility_text``
  → the in-memory sparse index.

Both spell out the *same four fields*.  ``benefits_text`` is derived from
``benefits[].description`` because DATA_SPEC §2.3 has no ``benefits_text``
property — the architecture names the component, the schema names its source.
"""

from __future__ import annotations

from typing import Any, Mapping


def scheme_text(scheme: Mapping[str, Any]) -> str:
    """Build the shared four-field corpus string for a canonical Scheme.

    Order is fixed (not merely stable) because it must be identical between
    the day a point was embedded and the day the BM25 cache is rebuilt.
    """
    benefits_text = " ".join(
        b.get("description", "") for b in scheme.get("benefits", ()) if b
    )
    parts = [
        scheme["name"],
        scheme["department"],
        benefits_text,
        scheme["eligibility_text"],
    ]
    return " ".join(part for part in parts if part)
