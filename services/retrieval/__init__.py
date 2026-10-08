"""Storage & Retrieval Layer — `services/retrieval/` (ARCHITECTURE §5.3).

Components:
    Qdrant Vector DB    collection `schemes`, point id = scheme_id,
                        1024-dim BAAI/bge-m3, cosine distance
    In-memory BM25      rebuildable cache derived from MongoDB; k1 = 1.2, b = 0.75
                        (never authoritative, version-stamped)
    MongoDB             canonical source of truth: schemes, scheme_revisions,
                        verification_logs, citizen_profiles, ingestion_audit
    Object Storage      immutable Web Agent snapshots at
                        snapshots/{scheme_id}/{job_id}.png, 90-day lifecycle

Single source of truth: MongoDB holds the canonical Scheme record; Qdrant holds
only the derived vector + payload index; BM25 is a rebuildable cache.

Must NOT own: canonical document truth outside MongoDB, rule evaluation,
TinyFish calls, or any LLM prompting.
"""
