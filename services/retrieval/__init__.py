"""Storage & Retrieval Layer — `services/retrieval/` (ARCHITECTURE §5.3).

Components:
    Qdrant Vector DB    collection `schemes`, 1024-dim BAAI/bge-m3, cosine
                        distance. Point id is a deterministic UUIDv5 of the
                        scheme_id (Qdrant rejects string ids), and the payload
                        carries `scheme_id` as the join key — see
                        `services.api.storage.point_id`.
    In-memory BM25      rebuildable cache derived from MongoDB; k1 = 1.2, b = 0.75
                        (never authoritative, version-stamped)
    Corpus text         `text.scheme_text` — the single four-field builder
                        shared by the embedder and BM25 so sparse and dense
                        recall cannot drift apart
    MongoDB             canonical source of truth: schemes, scheme_revisions,
                        verification_logs, citizen_profiles, ingestion_audit
    Object Storage      immutable Web Agent snapshots at
                        snapshots/{scheme_id}/{job_id}.png, 90-day lifecycle

Single source of truth: MongoDB holds the canonical Scheme record; Qdrant holds
only the derived vector + payload index; BM25 is a rebuildable cache.

Must NOT own: canonical document truth outside MongoDB, rule evaluation,
TinyFish calls, or any LLM prompting.
"""
