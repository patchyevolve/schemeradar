"""BUILD_ORDER task 2.7 — build the in-memory BM25 cache (ARCHITECTURE §5.3.2).

Validation command (BUILD_ORDER §4.5)::

    python -m tools.build_bm25              # version stamp printed
    python -m tools.build_bm25 --query "post matric"

The corpus is read from **MongoDB**, the canonical source of truth — never
from the seed files — so the cache always reflects what is actually indexed.
The build is written to a git-ignored cache file and stamped; a later run with
an unchanged stamp reuses it, any changed ``source.content_hash`` invalidates
it and forces a rebuild.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Sequence

REPO = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_CACHE = REPO / "cache" / "bm25_index.json"


class Bm25Error(Exception):
    """The corpus could not be read, or the cache was unusable."""


def _load_cache(path: pathlib.Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf8"))
    except (json.JSONDecodeError, OSError):
        return None  # a corrupt cache is simply rebuilt, never fatal


def build_index(db, cache_path: pathlib.Path, *, quiet: bool = False):
    """Build (or reuse) the index. Returns ``(index, rebuilt, reason)``."""
    from services.retrieval.bm25 import BM25Index, version_stamp

    docs = list(db.schemes.find({}))
    if not docs:
        raise Bm25Error("the `schemes` collection is empty — run `python -m tools.seed` first")

    stamp = version_stamp(docs)
    cached = _load_cache(cache_path)
    if cached is not None and cached.get("stamp") == stamp:
        try:
            return BM25Index.from_cache(cached), False, "stamp match"
        except (KeyError, ValueError, TypeError):
            pass  # unreadable cache → rebuild

    index = BM25Index.build(docs)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(index.to_cache()), encoding="utf8")
    reason = "no cache" if cached is None else "content_hash or params changed"
    return index, True, reason


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.build_bm25",
        description="Build the version-stamped in-memory BM25 cache.",
    )
    parser.add_argument(
        "--query",
        action="append",
        default=[],
        metavar="TEXT",
        help="run a lexical query against the built index (repeatable)",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--cache", type=pathlib.Path, default=DEFAULT_CACHE, help="cache file path"
    )
    parser.add_argument(
        "--rebuild", action="store_true", help="ignore any existing cache"
    )
    args = parser.parse_args(argv)

    try:
        from services.api.config import get_settings

        from pymongo import MongoClient

        settings = get_settings()
        client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
        try:
            client.admin.command("ping")
        except Exception as exc:
            raise Bm25Error(f"MongoDB unreachable at {settings.mongo_uri}: {exc}") from exc
        db = client[settings.mongo_db]

        if args.rebuild and args.cache.exists():
            args.cache.unlink()

        index, rebuilt, reason = build_index(db, args.cache)
        print(f"BM25 index over {len(index)} document(s)  k1={index.k1} b={index.b}")
        print(f"version stamp: {index.stamp}")
        print(f"cache: {args.cache.relative_to(REPO)} ({'rebuilt' if rebuilt else 'reused'} — {reason})")

        failures = 0
        for query in args.query:
            hits = index.search(query, top_k=args.top_k)
            print(f'\nquery "{query}" → {len(hits)} hit(s)')
            for rank, (scheme_id, score) in enumerate(hits, 1):
                print(f"  {rank}. {scheme_id}  score={score:.4f}")
            if not hits:
                failures += 1
        client.close()
        return 1 if failures else 0
    except Bm25Error as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
