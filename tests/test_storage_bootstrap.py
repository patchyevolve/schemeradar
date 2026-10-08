"""BUILD_ORDER Step 2 tasks 2.3 (indexes), 2.6 (payload projection), 2.8 (aux).

The projection tests are pure and always run. The index tests talk to the live
Mongo/Qdrant started by ``docker compose up -d`` and **skip** when neither is
reachable, so this suite stays green on a machine without Docker.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from services.api.storage import (
    AUX_COLLECTIONS,
    QDRANT_FIELD_SCHEMA,
    QDRANT_PAYLOAD_KEYS,
    project_qdrant_payload,
)

REPO = pathlib.Path(__file__).resolve().parents[1]
SEED_PATH = REPO / "seeds" / "sch_delhi_post_matric_scholarship_sc_st_obc_2026.json"


def _seed() -> dict:
    return json.loads(SEED_PATH.read_text(encoding="utf8"))


# --- task 2.6: Qdrant payload projection (DATA_SPEC §3.3) -------------------


def test_projection_is_exactly_the_section_33_key_set():
    """7 filterable + 5 stored = 12 keys — no more, no less."""
    payload = project_qdrant_payload(_seed())
    assert set(payload) == set(QDRANT_PAYLOAD_KEYS)
    assert len(payload) == 12
    assert len(QDRANT_PAYLOAD_KEYS) == len(set(QDRANT_PAYLOAD_KEYS))


def test_projection_values_are_verbatim_from_the_canonical_scheme():
    scheme = _seed()
    payload = project_qdrant_payload(scheme)
    for key in QDRANT_PAYLOAD_KEYS:
        assert payload[key] == scheme[key], key


def test_projection_drops_everything_else():
    """The Qdrant payload is a strict *subset* — nothing else may leak in."""
    scheme = _seed()
    payload = project_qdrant_payload(scheme)
    leaked = set(scheme) - set(payload)
    assert len(scheme) == 47 and len(payload) == 12 and len(leaked) == 35
    # None of the bulky/authoritative parts may reach the derived index.
    for forbidden in (
        "eligibility_text",
        "required_documents",
        "soft_clauses",
        "domain_gates",
        "conditional_gate_overrides",
        "allowed_categories",
        "source",
        "gates",
    ):
        assert forbidden not in payload


def test_projection_fails_closed_when_a_source_field_is_missing():
    """A missing field must raise, not ship a point nobody can filter on."""
    scheme = _seed()
    del scheme["verification_status"]
    with pytest.raises(KeyError):
        project_qdrant_payload(scheme)


# --- live infrastructure ----------------------------------------------------


def _settings():
    from services.api.config import get_settings

    return get_settings()


@pytest.fixture(scope="module")
def mongo_db():
    from pymongo import MongoClient

    s = _settings()
    client = MongoClient(s.mongo_uri, serverSelectionTimeoutMS=2000)
    try:
        client.admin.command("ping")
    except Exception as exc:  # pragma: no cover - depends on docker
        pytest.skip(f"MongoDB not reachable: {exc}")
    yield client[s.mongo_db]
    client.close()


@pytest.fixture(scope="module")
def qdrant():
    from qdrant_client import QdrantClient

    s = _settings()
    client = QdrantClient(s.qdrant_url, timeout=5)
    try:
        client.get_collections()
    except Exception as exc:  # pragma: no cover - depends on docker
        pytest.skip(f"Qdrant not reachable: {exc}")
    yield client
    client.close()


# --- task 2.3: MongoDB indexes ---------------------------------------------


def test_mongo_schemes_has_the_four_declared_indexes(mongo_db):
    from services.api.storage import ensure_mongo_indexes

    names = ensure_mongo_indexes(mongo_db)
    for expected in (
        "scheme_id_1",
        "is_active_1",
        "domicile_state_1",
        "source.content_hash_1",
    ):
        assert expected in names, names


def test_mongo_scheme_id_is_the_only_unique_index(mongo_db):
    """`is_active`/`domicile_state`/`content_hash` are read-path, not keys."""
    unique_fields = set()
    for spec in mongo_db.schemes.list_indexes():
        if spec.get("unique"):
            unique_fields.add(next(iter(spec["key"])))
    assert unique_fields == {"scheme_id"}, unique_fields


def test_mongo_unique_scheme_id_is_enforced(mongo_db):
    """Behavioural, not just declarative: a duplicate must raise."""
    from pymongo.errors import DuplicateKeyError

    mongo_db.schemes.delete_one({"scheme_id": "__index_probe__"})
    try:
        mongo_db.schemes.insert_one({"scheme_id": "__index_probe__"})
        with pytest.raises(DuplicateKeyError):
            mongo_db.schemes.insert_one({"scheme_id": "__index_probe__"})
    finally:
        mongo_db.schemes.delete_one({"scheme_id": "__index_probe__"})


# --- task 2.8: auxiliary collections ---------------------------------------


def test_aux_collections_exist_with_their_indexes(mongo_db):
    from services.api.storage import ensure_aux_collections

    created = ensure_aux_collections(mongo_db)
    assert set(created) == set(AUX_COLLECTIONS)
    present = set(mongo_db.list_collection_names())
    assert set(AUX_COLLECTIONS) <= present, present
    for coll, indexes in created.items():
        assert "_id_" in indexes, (coll, indexes)


def test_aux_primary_keys_are_unique(mongo_db):
    from services.api.storage import ensure_aux_collections

    ensure_aux_collections(mongo_db)
    for coll, spec in {
        "scheme_revisions": "revision_id_1",
        "verification_logs": "verification_job_id_1",
        "ingestion_audit": "cycle_id_1",
    }.items():
        names = {d["name"] for d in mongo_db[coll].list_indexes()}
        assert spec in names, (coll, names)
        assert (
            mongo_db[coll].index_information()[spec].get("unique") is True
        ), f"{coll}.{spec} is not unique"


def test_citizen_profiles_is_created_without_invented_keys(mongo_db):
    """DATA_SPEC §3.1 defines no profile identifier — we must not invent one."""
    from services.api.storage import ensure_aux_collections

    ensure_aux_collections(mongo_db)
    assert "citizen_profiles" in mongo_db.list_collection_names()
    names = {d["name"] for d in mongo_db.citizen_profiles.list_indexes()}
    assert names == {"_id_"}, names


# --- task 2.3 / 2.6: Qdrant -------------------------------------------------


def test_qdrant_collection_geometry(qdrant):
    """Point id = scheme_id, 1024-d bge-m3, cosine (ARCHITECTURE §5.3.1)."""
    from services.api.storage import QDRANT_DISTANCE, QDRANT_VECTOR_SIZE, ensure_qdrant_collection

    s = _settings()
    ensure_qdrant_collection(qdrant, s.qdrant_collection)
    info = qdrant.get_collection(s.qdrant_collection)
    vectors = info.config.params.vectors
    assert vectors.size == QDRANT_VECTOR_SIZE == 1024
    assert vectors.distance.value == QDRANT_DISTANCE


def test_qdrant_payload_indexes_cover_all_seven_filterable_fields(qdrant):
    from services.api.storage import ensure_qdrant_collection

    s = _settings()
    ensure_qdrant_collection(qdrant, s.qdrant_collection)
    info = qdrant.get_collection(s.qdrant_collection)
    assert set(info.payload_schema) == set(QDRANT_FIELD_SCHEMA) == {
        "domicile_state",
        "category",
        "scheme_type",
        "department",
        "fiscal_year",
        "is_active",
        "verification_status",
    }


def test_bootstrap_is_idempotent(mongo_db, qdrant):
    from services.api.storage import ensure_qdrant_collection, ensure_mongo_indexes

    ensure_mongo_indexes(mongo_db)
    first = ensure_mongo_indexes(mongo_db)
    s = _settings()
    ensure_qdrant_collection(qdrant, s.qdrant_collection)
    again = ensure_qdrant_collection(qdrant, s.qdrant_collection)

    assert again["collection_created"] is False
    assert again["payload_index_created"] == []
    assert first == ensure_mongo_indexes(mongo_db)
