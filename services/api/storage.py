"""Data-layer bootstrap: MongoDB indexes, Qdrant collection, payload projection.

Implements BUILD_ORDER Step 2 tasks **2.3** (indexes), **2.6** (Qdrant payload
projection) and **2.8** (auxiliary collections).

Ownership boundary (ARCHITECTURE §5.3): MongoDB is the canonical document
truth; Qdrant holds only a *derived* vector + payload subset (DATA_SPEC §3.3)
and is never authoritative; BM25 is a rebuildable cache.  Nothing in this
module scores, filters eligibility or evaluates a rule — it only shapes the
storage so the rest of the system has somewhere to put validated documents.

Evidence: ARCHITECTURE §5.3.1 / §5.3.3, DATA_SPEC §3.3, §8, BUILD_ORDER §4.2
tasks 2.3 / 2.6 / 2.8.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterable, Mapping

# --- frozen constants (ARCHITECTURE §5.3.1) --------------------------------
# 1024-dim BAAI/bge-m3 embeddings, cosine distance.  The point id is derived
# deterministically from the canonical `scheme_id` (see `point_id`), so a
# re-seed overwrites rather than duplicates.
QDRANT_VECTOR_SIZE = 1024
QDRANT_DISTANCE = "Cosine"

# Namespace for `point_id` — the RFC 4122 well-known NAMESPACE_DNS constant,
# exactly as ARCHITECTURE §5.3.1 now specifies.  Being a published constant it
# is fixed forever: changing it would mint new point ids and orphan every
# existing point.
POINT_NAMESPACE = uuid.NAMESPACE_DNS

# --- task 2.3: MongoDB indexes on `schemes` --------------------------------
# `scheme_id` is the natural key and therefore unique.  The other three are
# read-path indexes named verbatim by BUILD_ORDER 2.3; none of them is
# declared unique, because two sources may legitimately share an
# `is_active`/`domicile_state`/`content_hash` value.
# `content_hash` lives at `source.content_hash` (DATA_SPEC §2.3 `SourceRef`).
SCHEMES_INDEXES: tuple[tuple[str | tuple[str, ...], dict[str, Any]], ...] = (
    ("scheme_id", {"unique": True}),
    ("is_active", {}),
    # array with set semantics (["ALL"] = pan-India) ⇒ multikey index
    ("domicile_state", {}),
    ("source.content_hash", {}),
)

# --- task 2.8: auxiliary collections (DATA_SPEC §8) ------------------------
# Only indexes the specification actually justifies are declared:
#   * the PK of each record is unique,
#   * the FK back to `schemes` is indexed for joins,
#   * `scheme_revisions.revision` is monotonic *per scheme*, so the compound
#     (scheme_id, revision) is unique — that is what "monotonic" buys us.
# `citizen_profiles` deliberately gets none: DATA_SPEC §3.1's ProfileContext
# carries no identifier field, so inventing a `profile_id` here would be a
# schema change through the back door.  Persistence design is Step 4's call.
AUX_COLLECTIONS: dict[str, tuple[tuple[str | tuple[str, ...], dict[str, Any]], ...]] = {
    "scheme_revisions": (
        ("revision_id", {"unique": True}),
        ("scheme_id", {}),
        (("scheme_id", "revision"), {"unique": True}),
    ),
    "verification_logs": (
        ("verification_job_id", {"unique": True}),
        ("scheme_id", {}),
        ("started_at", {}),
    ),
    "ingestion_audit": (
        ("cycle_id", {"unique": True}),
        ("scheme_id", {}),
        ("at", {}),
    ),
    "citizen_profiles": (),
}

# --- task 2.6: Qdrant payload projection (DATA_SPEC §3.3) ------------------
# The payload is a strict subset of the canonical Scheme; nothing in Qdrant
# may be authoritative.  §3.3 lists 13 keys: 7 filterable, 5 stored, plus
# `scheme_id` as the join key back to MongoDB.
#
# WHY `scheme_id` has to be in the payload: Qdrant only accepts an unsigned
# integer or a UUID as a point id —
#
#     400 ... value sch_delhi_... is not a valid point id, valid values are
#           either an unsigned integer or a UUID
#
# so the point id is `uuid5(NAMESPACE_DNS, scheme_id)` (ARCHITECTURE §5.3.1)
# and the real `scheme_id` rides along in the payload as the join key.  The
# payload remains a strict subset of Scheme — `scheme_id` is a Scheme property
# — and Qdrant still holds nothing authoritative.
QDRANT_FILTERABLE: tuple[str, ...] = (
    "domicile_state",
    "category",
    "scheme_type",
    "department",
    "fiscal_year",
    "is_active",
    "verification_status",
)
QDRANT_STORED: tuple[str, ...] = (
    "name",
    "income_ceiling_annual",
    "min_age",
    "max_age",
    "benefits_summary",
)
QDRANT_PAYLOAD_KEYS: tuple[str, ...] = ("scheme_id",) + QDRANT_FILTERABLE + QDRANT_STORED

# Qdrant payload-index type per filterable field.  `domicile_state` is an
# array of state codes, which Qdrant indexes multikey under a keyword schema.
QDRANT_FIELD_SCHEMA: dict[str, str] = {
    "domicile_state": "keyword",
    "category": "keyword",
    "scheme_type": "keyword",
    "department": "keyword",
    "fiscal_year": "keyword",
    "is_active": "bool",
    "verification_status": "keyword",
}


def point_id(scheme_id: str) -> str:
    """Deterministic Qdrant point id: ``uuid5(NAMESPACE_DNS, scheme_id)``.

    Qdrant accepts only an unsigned integer or a UUID as a point id, so the
    string ``scheme_id`` cannot be one.  ``uuid5`` over the well-known
    ``NAMESPACE_DNS`` constant makes the mapping reproducible on every machine
    with no lookup table — re-seeding overwrites the same point instead of
    duplicating it.  The inverse is not needed: ``scheme_id`` itself travels
    in the payload as the join key back to MongoDB (DATA_SPEC §3.3).
    """
    return str(uuid.uuid5(POINT_NAMESPACE, scheme_id))


def project_qdrant_payload(scheme: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the Qdrant payload from a canonical Scheme (DATA_SPEC §3.3).

    Fails closed: a missing source field raises ``KeyError`` rather than
    silently shipping a point that cannot be filtered on.
    """
    payload = {key: scheme[key] for key in QDRANT_PAYLOAD_KEYS}
    if set(payload) != set(QDRANT_PAYLOAD_KEYS):  # pragma: no cover - defensive
        raise ValueError("payload projection diverged from DATA_SPEC §3.3")
    return payload


def _index_specs(
    specs: Iterable[tuple[str | tuple[str, ...], Mapping[str, Any]]],
) -> list[tuple[Any, dict[str, Any]]]:
    """Normalise ("field" | ("a","b"), opts) into pymongo's (keys, opts)."""
    out: list[tuple[Any, dict[str, Any]]] = []
    for keys, opts in specs:
        out.append((list(keys) if isinstance(keys, tuple) else keys, dict(opts)))
    return out


def ensure_mongo_indexes(db) -> list[str]:
    """Create the four `schemes` indexes (task 2.3).  Idempotent.

    Returns a human-readable list of the index names that now exist.
    """
    for keys, opts in _index_specs(SCHEMES_INDEXES):
        db.schemes.create_index(keys, **opts)
    return sorted(doc["name"] for doc in db.schemes.list_indexes())


def ensure_aux_collections(db) -> dict[str, list[str]]:
    """Create the four auxiliary collections with their indexes (task 2.8).

    Idempotent: existing indexes are left untouched, missing ones created.
    """
    result: dict[str, list[str]] = {}
    for name, specs in AUX_COLLECTIONS.items():
        coll = db[name]
        for keys, opts in _index_specs(specs):
            coll.create_index(keys, **opts)
        # create_collection on an empty spec list still materialises the
        # collection so `listCollections` shows it even before first insert.
        if name not in db.list_collection_names():
            db.create_collection(name)
        result[name] = sorted(doc["name"] for doc in coll.list_indexes())
    return result


def ensure_qdrant_collection(client, name: str) -> dict[str, Any]:
    """Create the `schemes` collection and its payload indexes (tasks 2.3/2.6).

    Idempotent — re-running against a live Qdrant is a no-op that reports
    what already existed.  Returns a summary dict for CLI/test assertions.
    """
    from qdrant_client.models import Distance, PayloadSchemaType, VectorParams

    existing = {c.name for c in client.get_collections().collections}
    created = name not in existing
    if created:
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(
                size=QDRANT_VECTOR_SIZE, distance=Distance.COSINE
            ),
        )

    collection = client.get_collection(name)
    have = set(getattr(collection, "payload_schema", None) or {})
    created_fields: list[str] = []
    for field, schema_name in QDRANT_FIELD_SCHEMA.items():
        if field in have:
            continue
        field_schema = (
            PayloadSchemaType.KEYWORD
            if schema_name == "keyword"
            else PayloadSchemaType.BOOL
        )
        client.create_payload_index(
            collection_name=name, field_name=field, field_schema=field_schema
        )
        created_fields.append(field)

    return {
        "collection": name,
        "collection_created": created,
        "vector_size": QDRANT_VECTOR_SIZE,
        "distance": QDRANT_DISTANCE,
        "payload_indexed": sorted(QDRANT_FIELD_SCHEMA),
        "payload_index_created": sorted(created_fields),
    }


def summarize_storage(db, qdrant_client, qdrant_collection: str) -> dict[str, Any]:
    """One call that proves tasks 2.3, 2.6 and 2.8 against live services."""
    return {
        "mongo": {
            "schemes_indexes": ensure_mongo_indexes(db),
            "aux_collections": ensure_aux_collections(db),
        },
        "qdrant": ensure_qdrant_collection(qdrant_client, qdrant_collection),
        "payload_keys": list(QDRANT_PAYLOAD_KEYS),
    }
