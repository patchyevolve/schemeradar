"""BUILD_ORDER task 2.5 — validate → embed → upsert, idempotent on content_hash.

Validation command (BUILD_ORDER §4.5)::

    python -m tools.seed --dry-run       # "2 to insert, 0 to update"
    python -m tools.seed                 # writes

Per document the pipeline is:

1. **L1** — JSON Schema validation against ``schemas/scheme.schema.json``
   (draft 2020-12, with ``format`` checkers enabled, so ``date-time``/``uri``
   are really checked rather than skipped).
2. **L3** — Pydantic ``services.api.models.Scheme``: the cross-field
   invariants V-CF1/V-CF8/V-CF9 plus field/enum parity.
3. **Plan** — compare ``source.content_hash`` with the stored document to
   decide ``insert`` / ``update`` / ``unchanged``.
4. **Embed** — only when a write is actually needed, so a no-op re-seed never
   loads the 2.3 GB model.
5. **Upsert** — canonical document into MongoDB, derived point into Qdrant.

Idempotency: an unchanged ``content_hash`` skips the document entirely, and
Qdrant points are addressed by ``point_id(scheme_id)`` (deterministic UUIDv5),
so even a forced rewrite overwrites in place instead of duplicating.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Sequence

REPO = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO / "schemas" / "scheme.schema.json"
DEFAULT_SEEDS_DIR = REPO / "seeds"


class SeedError(Exception):
    """A document failed validation, or a datastore was unreachable."""


# --- validation (L1 + L3) ---------------------------------------------------


def _schema_validator():
    from jsonschema import Draft202012Validator

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf8"))
    return Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    )


def validate_document(doc: dict[str, Any], validator, where: str) -> None:
    """L1 then L3. Raises :class:`SeedError` with a readable message."""
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        path = ".".join(str(p) for p in first.absolute_path) or "<root>"
        raise SeedError(
            f"{where}: L1 JSON Schema rejected `{path}`: {first.message} "
            f"({len(errors)} error(s) total)"
        )
    try:
        from services.api.models import Scheme

        Scheme.model_validate(doc)
    except Exception as exc:  # pydantic.ValidationError
        raise SeedError(f"{where}: L3 ladder rejected the document: {exc}") from exc


def load_seeds(directory: pathlib.Path) -> list[tuple[pathlib.Path, dict[str, Any]]]:
    paths = sorted(directory.glob("*.json"))
    if not paths:
        raise SeedError(f"no *.json seeds found in {directory}")
    out: list[tuple[pathlib.Path, dict[str, Any]]] = []
    for path in paths:
        try:
            doc = json.loads(path.read_text(encoding="utf8"))
        except json.JSONDecodeError as exc:
            raise SeedError(f"{path.name}: not valid JSON: {exc}") from exc
        out.append((path, doc))
    return out


# --- planning ---------------------------------------------------------------


def content_hash_of(doc: dict[str, Any]) -> str:
    try:
        return doc["source"]["content_hash"]
    except (KeyError, TypeError) as exc:
        raise SeedError(f"{doc.get('scheme_id', '<unknown>')}: missing source.content_hash") from exc


def plan_document(doc: dict[str, Any], stored: dict[str, Any] | None) -> str:
    """``insert`` (absent) · ``update`` (hash moved) · ``unchanged``."""
    if stored is None:
        return "insert"
    return "update" if stored.get("source", {}).get("content_hash") != content_hash_of(doc) else "unchanged"


# --- datastores -------------------------------------------------------------


def _mongo(settings):
    from pymongo import MongoClient

    client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    try:
        client.admin.command("ping")
    except Exception as exc:
        raise SeedError(f"MongoDB unreachable at {settings.mongo_uri}: {exc}") from exc
    return client, client[settings.mongo_db]


def _qdrant(settings):
    from qdrant_client import QdrantClient

    client = QdrantClient(settings.qdrant_url, timeout=30)
    try:
        client.get_collections()
    except Exception as exc:
        raise SeedError(f"Qdrant unreachable at {settings.qdrant_url}: {exc}") from exc
    return client


def _bootstrap(db, qdrant_client, collection: str) -> None:
    """Ensure indexes / collection exist before writing (tasks 2.3, 2.6, 2.8)."""
    from services.api.storage import (
        ensure_aux_collections,
        ensure_mongo_indexes,
        ensure_qdrant_collection,
    )

    ensure_mongo_indexes(db)
    ensure_aux_collections(db)
    ensure_qdrant_collection(qdrant_client, collection)


def _point_exists(qdrant_client, collection: str, pid: str) -> bool:
    return bool(qdrant_client.retrieve(collection, ids=[pid], with_payload=False))


# --- run --------------------------------------------------------------------


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.seed",
        description="Validate → embed → upsert the canonical seed instances.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print the plan without writing anything",
    )
    parser.add_argument(
        "--seeds",
        type=pathlib.Path,
        default=DEFAULT_SEEDS_DIR,
        help=f"directory of *.json seed documents (default: {DEFAULT_SEEDS_DIR})",
    )
    args = parser.parse_args(argv)

    try:
        validator = _schema_validator()
        docs = load_seeds(args.seeds)
        print(f"Validated {len(docs)} document(s) against {SCHEMA_PATH.name}")

        from services.api.config import get_settings
        from services.api.storage import point_id, project_qdrant_payload

        settings = get_settings()
        mongo, db = _mongo(settings)
        qdrant_client = _qdrant(settings)

        plan: list[tuple[dict[str, Any], str, str]] = []
        for path, doc in docs:
            validate_document(doc, validator, path.name)
            stored = db.schemes.find_one(
                {"scheme_id": doc["scheme_id"]}, {"source.content_hash": 1}
            )
            plan.append((doc, plan_document(doc, stored), point_id(doc["scheme_id"])))

        to_insert = sum(1 for _, op, _ in plan if op == "insert")
        to_update = sum(1 for _, op, _ in plan if op == "update")
        unchanged = sum(1 for _, op, _ in plan if op == "unchanged")

        # What actually needs writing:
        #   * changed documents → Mongo + Qdrant
        #   * unchanged documents whose Qdrant point is missing → Qdrant only
        #     (the vector index can be rebuilt from scratch while Mongo stays
        #     current; a no-op re-seed must still leave Qdrant complete, or
        #     Gate G2's filtered query would come up empty).
        writes: list[tuple[dict[str, Any], bool]] = []
        repairs = 0
        for doc, op, pid in plan:
            if op != "unchanged":
                writes.append((doc, True))
            elif not _point_exists(qdrant_client, settings.qdrant_collection, pid):
                writes.append((doc, False))
                repairs += 1

        print(f"{to_insert} to insert, {to_update} to update")
        if unchanged:
            print(f"{unchanged} unchanged (content_hash match)")
        if repairs:
            print(f"{repairs} missing Qdrant point(s) to restore")

        if args.dry_run:
            print("dry run — nothing written")
            return 0

        _bootstrap(db, qdrant_client, settings.qdrant_collection)

        if writes:
            from services.api.embedding import Embedder
            from services.retrieval.text import scheme_text

            embedder = Embedder()
            print(f"Embedding {len(writes)} document(s) with {embedder.model_name} …")
            vectors = embedder.encode([scheme_text(doc) for doc, _ in writes])
        else:
            vectors = []

        from qdrant_client.models import PointStruct

        mongo_writes = 0
        for (doc, write_mongo), vector in zip(writes, vectors):
            if write_mongo:
                db.schemes.replace_one({"scheme_id": doc["scheme_id"]}, doc, upsert=True)
                mongo_writes += 1
            qdrant_client.upsert(
                collection_name=settings.qdrant_collection,
                points=[
                    PointStruct(
                        id=point_id(doc["scheme_id"]),
                        vector=vector,
                        payload=project_qdrant_payload(doc),
                    )
                ],
            )

        print(
            f"wrote {mongo_writes} document(s) to MongoDB and "
            f"{len(writes)} point(s) to Qdrant"
        )
        return 0
    except SeedError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
