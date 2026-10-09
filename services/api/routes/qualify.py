"""``POST /api/v1/profile/qualify`` — the dual-path discovery/search route.

ARCHITECTURE §5.2.1:

    Mode 1 (Home Screen Discovery)  q absent/empty
        -> synthesize a search string from the ProfileContext
        -> hybrid retrieval -> scorer gates against ProfileContext -> UI

    Mode 2 (Keyword Search)         q present
        -> hybrid retrieval on the raw query text q
        -> scorer intercepts and evaluates against ProfileContext -> UI

Step 3 scaffolds the route, the response envelope and the *shape* of the
pipeline.  ``retrieve_candidates`` (BUILD_ORDER 4.4) and ``score_candidates``
(BUILD_ORDER 4.1 / 4.7) are deliberately empty here: **no Playwright, no LLM
and no scoring math runs in this file.**  Their signatures are the typed
boundary Step 4 implements behind.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, computed_field

from services.api.models import ProfileContext

router = APIRouter(prefix="/api/v1", tags=["qualification"])

__all__ = ["router", "QualificationResponse", "synthesize_profile_query",
           "retrieve_candidates", "score_candidates"]


# --------------------------------------------------------------------------
# Response envelope — ARCHITECTURE §5.2.1
# --------------------------------------------------------------------------
class QualificationResponse(BaseModel):
    """What ``POST /api/v1/profile/qualify`` returns.

    ``matches`` is ``SchemeMatch[]`` (DATA_SPEC §3.2).  The typed
    ``SchemeMatch`` model is not authored yet — it is the Score Assembler's
    output contract and lands with BUILD_ORDER 4.7 — so the scaffold keeps
    the items as dictionaries and documents the shape at the contract level.
    """

    mode: Literal["discovery", "search"] = Field(
        description="Mode 1 (discovery) or Mode 2 (search); decided by `q` alone."
    )
    query: str = Field(
        description="Resolved search string: `q` verbatim, or the profile-synthesized string."
    )
    matches: list[dict] = Field(
        default_factory=list,
        description="Ranked SchemeMatch[] with score_breakdown; empty until BUILD_ORDER Step 4.",
    )

    @computed_field  # type: ignore[prop-define]
    @property
    def count(self) -> int:
        """Number of matches — derived, never client-trusted."""
        return len(self.matches)


# --------------------------------------------------------------------------
# Pipeline placeholders (implemented in BUILD_ORDER Step 4)
# --------------------------------------------------------------------------
def synthesize_profile_query(profile: ProfileContext) -> str:
    """Mode 1: build a search string from the profile's discriminating fields.

    STUB — joins state, education and category (ARCHITECTURE §5.2.1).  Step 4
    replaces the joining rule with real intent synthesis; the contract (one
    ``ProfileContext`` in, one search string out) does not change.
    """
    parts = (profile.domicile_state, profile.education_level, profile.social_category)
    # `.value` for the StrEnum fields, verbatim for `domicile_state` (a plain
    # `str` alias); underscores are separated so the string reads as language.
    return " ".join(str(getattr(p, "value", p)).replace("_", " ") for p in parts)


async def retrieve_candidates(*, profile: ProfileContext, query: str) -> list[dict]:
    """Hybrid Retrieval placeholder — BUILD_ORDER 4.4 (Step 4).

    Will fan out to Qdrant (dense, top-50) and the in-memory BM25 index
    (sparse, top-50), fuse with RRF ``k = 60`` and apply payload filters
    after fusion.  Returns no candidates until then.
    """
    _ = (profile, query)
    return []


async def score_candidates(
    *, profile: ProfileContext, candidates: list[dict], query: str
) -> list[dict]:
    """Deterministic Scoring placeholder — BUILD_ORDER 4.1 / 4.7 (Step 4).

    Will evaluate the 7 hard gates against ``profile`` (``S_det`` +
    ``gate_trace``), assemble ``Score`` and assign a band.  Returns no
    matches until then.
    """
    _ = (profile, candidates, query)
    return []


# --------------------------------------------------------------------------
# Route
# --------------------------------------------------------------------------
@router.post(
    "/profile/qualify",
    response_model=QualificationResponse,
    summary="Unified discovery / search qualification",
    description=(
        "Accepts a ProfileContext body plus an optional `q` query string and "
        "returns a QualificationResponse (ARCHITECTURE §5.2.1). "
        "Mode 1 (discovery) when `q` is absent/empty; Mode 2 (search) when "
        "`q` is present. Retrieval and scoring are Step 4 placeholders, so "
        "`matches` is currently always empty."
    ),
)
async def qualify(
    profile: ProfileContext,
    q: str | None = Query(
        default=None,
        description="Optional natural language search string",
    ),
) -> QualificationResponse:
    """Run the dual-path discovery/search pipeline (ARCHITECTURE §5.2.1).

    A whitespace-only ``q`` counts as empty, so ``?q=`` behaves as Mode 1.
    """
    # -- 1. Resolve the search string -----------------------------------
    #    The *only* line that distinguishes Mode 1 from Mode 2.
    q_norm = (q or "").strip()
    if q_norm:
        mode, search_string = "search", q_norm
    else:
        mode, search_string = "discovery", synthesize_profile_query(profile)

    # -- 2. Hybrid Retrieval (Qdrant dense + BM25 sparse, RRF) ----------
    #    BUILD_ORDER 4.4 — placeholder until Step 4.
    candidates = await retrieve_candidates(profile=profile, query=search_string)

    # -- 3. Deterministic Scoring ---------------------------------------
    #    BUILD_ORDER 4.1 / 4.7 — placeholder until Step 4.
    matches = await score_candidates(
        profile=profile, candidates=candidates, query=search_string
    )

    # -- 4. Response ----------------------------------------------------
    return QualificationResponse(mode=mode, query=search_string, matches=matches)
