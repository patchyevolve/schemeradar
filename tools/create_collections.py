"""BUILD_ORDER tasks 2.3 + 2.8 — create indexes, the Qdrant collection, aux collections.

Thin CLI over :mod:`services.api.storage`; every step is idempotent, so this
is safe to run at any time.  ``python -m tools.seed`` runs the same bootstrap
before writing, but keeping the command separate (a) matches the Quickstart
in ``README.md`` and (b) lets the data layer be stood up before any document
exists.

    python -m tools.create_collections
"""

from __future__ import annotations

import json
import sys
from typing import Sequence


def run(argv: Sequence[str] | None = None) -> int:
    try:
        from pymongo import MongoClient
        from qdrant_client import QdrantClient

        from services.api.config import get_settings
        from services.api.storage import summarize_storage

        settings = get_settings()
        mongo = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
        try:
            mongo.admin.command("ping")
        except Exception as exc:
            print(f"ERROR: MongoDB unreachable at {settings.mongo_uri}: {exc}", file=sys.stderr)
            return 1
        try:
            qdrant = QdrantClient(settings.qdrant_url, timeout=10)
            qdrant.get_collections()
        except Exception as exc:
            print(f"ERROR: Qdrant unreachable at {settings.qdrant_url}: {exc}", file=sys.stderr)
            return 1

        summary = summarize_storage(
            mongo[settings.mongo_db], qdrant, settings.qdrant_collection
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except Exception as exc:  # pragma: no cover - defensive
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
