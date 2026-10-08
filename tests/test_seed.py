"""BUILD_ORDER task 2.5 — seed pipeline: validate → plan → upsert.

Pure planning/validation tests always run; the end-to-end idempotency test
talks to the live stack and skips when Docker is down.  It embeds with the
already-cached `BAAI/bge-m3` weights (~2.3 GB on first ever run).
"""

from __future__ import annotations

import json
import pathlib

import pytest

from tools.seed import (
    SeedError,
    content_hash_of,
    load_seeds,
    plan_document,
    run,
    validate_document,
)
from tools.seed import _schema_validator

REPO = pathlib.Path(__file__).resolve().parents[1]
SEEDS = REPO / "seeds"


def _doc(name: str = "sch_delhi_post_matric_scholarship_sc_st_obc_2026.json") -> dict:
    return json.loads((SEEDS / name).read_text(encoding="utf8"))


# --- validation (L1 + L3) ---------------------------------------------------


def test_both_canonical_seeds_validate():
    validator = _schema_validator()
    for path, doc in load_seeds(SEEDS):
        validate_document(doc, validator, path.name)  # must not raise


def test_l1_rejects_a_missing_required_key():
    validator = _schema_validator()
    doc = _doc()
    del doc["verification_status"]
    with pytest.raises(SeedError, match="L1 JSON Schema"):
        validate_document(doc, validator, "broken.json")


def test_l3_rejects_a_cross_field_invariant_breach():
    """Pydantic catches what JSON Schema cannot — V-CF1 age bounds."""
    validator = _schema_validator()
    doc = _doc()
    doc["min_age"], doc["max_age"] = 60, 30  # schema-valid, invariant-invalid
    with pytest.raises(SeedError, match="L3 ladder"):
        validate_document(doc, validator, "broken.json")


def test_a_non_json_seed_file_fails_loudly(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf8")
    with pytest.raises(SeedError, match="not valid JSON"):
        load_seeds(tmp_path)


def test_an_empty_seed_directory_fails_loudly(tmp_path):
    with pytest.raises(SeedError, match="no \\*\\.json seeds"):
        load_seeds(tmp_path)


# --- planning (content_hash idempotency) -----------------------------------


def test_plan_inserts_when_the_document_is_absent():
    assert plan_document(_doc(), None) == "insert"


def test_plan_is_unchanged_when_content_hash_matches():
    doc = _doc()
    stored = {"scheme_id": doc["scheme_id"], "source": dict(doc["source"])}
    assert plan_document(doc, stored) == "unchanged"


def test_plan_updates_when_content_hash_moves():
    doc = _doc()
    stored = {"scheme_id": doc["scheme_id"], "source": {"content_hash": "sha256:" + "a" * 64}}
    assert plan_document(doc, stored) == "update"


def test_content_hash_is_read_from_source():
    assert content_hash_of(_doc()) == _doc()["source"]["content_hash"]
    with pytest.raises(SeedError, match="missing source.content_hash"):
        content_hash_of({"scheme_id": "x"})


# --- end-to-end -------------------------------------------------------------


def test_dry_run_never_writes(capsys):
    assert run(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "to insert" in out and "dry run — nothing written" in out


def test_seeding_is_idempotent_on_content_hash(capsys):
    """DoD: `content_hash` unchanged ⇒ re-seed is a no-op."""
    first = run([])
    assert first == 0, capsys.readouterr().out

    capsys.readouterr()
    second = run([])
    out = capsys.readouterr().out
    assert second == 0, out
    assert "0 to insert, 0 to update" in out
    assert "2 unchanged (content_hash match)" in out
    assert "wrote 0 document(s)" in out


def test_qdrant_holds_exactly_one_point_per_scheme():
    """Re-running must not duplicate: 2 seeds ⇒ 2 points, forever."""
    from qdrant_client import QdrantClient

    from services.api.config import get_settings

    settings = get_settings()
    client = QdrantClient(settings.qdrant_url, timeout=5)
    try:
        info = client.get_collection(settings.qdrant_collection)
    except Exception as exc:  # pragma: no cover - depends on docker
        pytest.skip(f"Qdrant not reachable: {exc}")
    assert info.points_count == 2, info.points_count
    assert info.indexed_vectors_count is None or info.points_count == 2
