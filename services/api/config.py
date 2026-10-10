"""Runtime configuration for the SchemeRadar API gateway.

Values are read from the process environment, optionally seeded by a `.env`
file at the repository root (see `.env.example`). None of these are
architectural constants — the frozen values live in ARCHITECTURE §9.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed, validated view of the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- datastores ---------------------------------------------------------
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "schemeradar"
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "schemes"

    # --- object storage (verification snapshots, 90-day lifecycle) ----------
    object_storage_endpoint: str = "http://localhost:9000"
    object_storage_bucket: str = "schemeradar-snapshots"
    # SigV4 credentials for PUT snapshots/{scheme_id}/{job_id}.png.
    # Empty => the snapshot upload is skipped (signal `snapshot_failed`), which
    # never changes the verdict (WORKFLOW §2.3 step 13).
    object_storage_access_key: str = ""
    object_storage_secret_key: str = ""

    # --- TinyFish (ARCHITECTURE §7) -----------------------------------------
    tinyfish_api_key: str = ""
    tinyfish_search_base_url: str = "https://api.search.tinyfish.ai"
    tinyfish_fetch_base_url: str = "https://api.fetch.tinyfish.ai"
    tinyfish_timeout_tier1_ms: int = 2000
    tinyfish_timeout_tier2_ms: int = 8000
    tinyfish_timeout_tier3_ms: int = 6000
    tinyfish_browser_concurrency: int = 3
    tinyfish_top_n_verified: int = 5

    # --- LLM tier -----------------------------------------------------------
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    llm_audit_timeout_ms: int = 900
    # Parser completion budget — deliberately *independent* of
    # ``llm_audit_timeout_ms`` (which prices a single Semantic Auditor
    # judgement, typically ~1 s).  The structured parser reads a multi-kilobyte
    # prompt and must be allowed a full generation, so coupling the two would
    # make every ingestion time out at the audit price.
    llm_completion_timeout_ms: int = 30000
    # Provider reasoning budget ("low" | "medium" | "high", "" = omit).
    # Groq's gpt-oss family spends hidden reasoning tokens against the
    # account TPM cap, so the effort level is part of the cost envelope.
    llm_reasoning_effort: str = "low"

    # --- security -----------------------------------------------------------
    admin_service_token: str = ""

    # --- server -------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings instance — the environment is read once per process."""
    return Settings()
