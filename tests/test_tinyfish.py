"""BUILD_ORDER Step 3 — TinyFish Tier 1/2/3 unit tests (Gate G3).

Every collaborator is injected, so this file is **offline by construction**:

* Tier 1/2  -> ``httpx.MockTransport`` (never the 44-char ``TINYFISH_API_KEY``)
* LLM       -> a fake ``llm=`` callable (``LLM_BASE_URL`` stays empty)
* Tier 3    -> an injected browser factory (no Playwright process) and a fake
               snapshot store (no MinIO request, no SigV4 round-trip)
* MongoDB   -> :class:`FakeCollection` (deterministic; no mongod needed)
* HTTP routes -> Starlette ``TestClient`` with the agent factory patched

Gate G3's "verifies a *live* gov.in portal" DoD box is **not** claimed here —
these are the mocked half of the ladder, the agent and the routes.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from datetime import date, datetime, timezone
from typing import Any, Callable

import httpx
import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SEED_PATH = REPO / "seeds" / "sch_pm_kisan_samman_nidhi_2019.json"

GOV_URL = "https://pmkisan.gov.in/"
SCH_ID = "sch_pm_kisan_samman_nidhi_2019"


# ===========================================================================
# helpers
# ===========================================================================
def run(coro: Any) -> Any:
    """pytest has no asyncio plugin here (services/api/requirements.txt), so
    every async assertion is driven by a private loop."""
    return asyncio.run(coro)


def seed() -> dict:
    return json.loads(SEED_PATH.read_text(encoding="utf8"))


class _VerificationLogs:
    """A distinct collection handle so ``verification_logs`` writes never land
    in ``db.logs`` (which parser tests use for ``ingestion_audit``)."""

    def __init__(self, owner: "FakeDB") -> None:
        self.owner = owner

    def insert_one(self, doc: dict) -> None:
        self.owner.verification_rows.append(dict(doc))


class FakeDB:
    """Stands in for the PyMongo ``Database``: records every write in order."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.prior: dict | None = None        # what ``find_one`` returns
        self.finds: list[dict] = []
        self.updates: list[dict] = []          # ``update_one`` filter + $set
        self.verification_rows: list[dict] = []  # DATA_SPEC §8.1

    @property
    def schemes(self) -> "FakeDB":
        return self

    @property
    def ingestion_audit(self) -> "FakeDB":
        return self

    @property
    def verification_logs(self) -> _VerificationLogs:
        return _VerificationLogs(self)

    def find_one(self, flt: dict, *args: Any, **kw: Any) -> dict | None:
        self.finds.append(dict(flt))
        return self.prior

    def update_one(self, flt: dict, update: dict, upsert: bool = False) -> None:
        self.updates.append({"filter": dict(flt), "update": dict(update)})

    def replace_one(self, flt: dict, doc: dict, upsert: bool = False) -> None:
        self.events.append(("replace_one", dict(doc)))

    def insert_one(self, doc: dict) -> None:
        self.events.append(("insert_one", dict(doc)))

    @property
    def documents(self) -> list[dict]:
        return [d for op, d in self.events if op == "replace_one"]

    @property
    def logs(self) -> list[dict]:
        return [d for op, d in self.events if op == "insert_one"]


@pytest.fixture
def db() -> FakeDB:
    return FakeDB()


# ===========================================================================
# Tier 1 — Search  (httpx.MockTransport)
# ===========================================================================
def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _search(handler, **kw):
    from services.tinyfish.client import TinyFishSearchClient

    return TinyFishSearchClient(
        api_key="test-key",
        base_url="https://tinyfish.test",
        http=_mock_client(handler),
        backoff_base_s=0.001,
        **kw,
    )


def test_search_filters_to_gov_in_hosts():
    from services.tinyfish.client import TinyFishSearchClient

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"  # live endpoint is GET /?query=…
        assert request.url.path == "/"
        seen["query"] = request.url.params.get("query", "")
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://pmkisan.gov.in/apply", "title": "A"},
                    {"url": "https://example.com/apply", "title": "ignored"},
                    {"url": "http://evil.nic.in.example.com/x", "title": "ignored"},
                    {"url": "https://services.punjab.gov.in/y", "title": "B"},
                ]
            },
        )

    async def main() -> list[str]:
        async with _search(handler) as c:
            hits = await c.discover(["pm kisan"], max_results=10)
        return [h.url for h in hits]

    assert run(main()) == ["https://pmkisan.gov.in/apply", "https://services.punjab.gov.in/y"]
    assert seen["query"] == "pm kisan"


def test_search_sends_both_auth_headers():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"results": []})

    run(_search(handler).discover(["q"]))
    assert seen.get("authorization") == "Bearer test-key"
    assert seen.get("x-api-key") == "test-key"


def test_search_429_backs_off_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"results": [{"url": GOV_URL}]})

    async def main() -> int:
        async with _search(handler) as c:
            hits = await c.discover(["q"])
        return len(hits)

    assert run(main()) == 1
    assert calls["n"] == 3  # retry x3 (ARCHITECTURE §7.4)


def test_search_401_is_terminal_no_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401)

    from services.tinyfish.client import TinyFishAuthError

    async def main() -> None:
        with pytest.raises(TinyFishAuthError):
            async with _search(handler) as c:
                await c.discover(["q"])

    run(main())
    assert calls["n"] == 1  # 401/403 never retry


def test_search_5xx_exhausts_retry_budget():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    from services.tinyfish.client import TinyFishUnavailable

    async def main() -> None:
        with pytest.raises(TinyFishUnavailable):
            async with _search(handler) as c:
                await c.discover(["q"])

    run(main())
    assert calls["n"] == 3


# ===========================================================================
# Tier 2 — Fetch
# ===========================================================================
def test_fetch_render_returns_markdown_and_content_hash():
    from services.tinyfish.client import TinyFishFetchClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/"  # live endpoint is POST /, not /render
        body = json.loads(request.content)
        assert body["urls"] == [GOV_URL]  # one-element array (§7.2)
        assert body["timeout_ms"] == 8000  # Tier-2 budget (§7.4)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": GOV_URL,
                        "final_url": GOV_URL,
                        "text": "# Pm Kisan\nbenefits",
                        "latency_ms": 42.0,
                        "format": "markdown",
                    }
                ],
                "errors": [],
            },
        )

    async def main():
        async with TinyFishFetchClient(
            api_key="k", base_url="https://f.test", http=_mock_client(handler)
        ) as c:
            return await c.render(GOV_URL)

    res = run(main())
    assert res.markdown.startswith("# Pm Kisan")
    assert res["http_status"] == 200  # a returned result means it rendered
    assert len(res.content_hash) == 64  # sha256 fallback is computed, not empty


def test_fetch_reports_the_status_when_the_portal_errors():
    """An error entry still carries the portal's own HTTP status (§7.2)."""
    from services.tinyfish.client import TinyFishFetchClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [],
                "errors": [{"url": GOV_URL, "error": "page_not_found", "status": 404}],
            },
        )

    async def main():
        async with TinyFishFetchClient(
            api_key="k", base_url="https://f.test", http=_mock_client(handler)
        ) as c:
            return await c.render(GOV_URL)

    res = run(main())
    assert res["http_status"] == 404
    assert res.markdown == ""


def test_fetch_rejects_non_gov_url_before_any_network_call():
    from services.tinyfish.client import SsrfBlocked, TinyFishFetchClient

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={})

    async def main() -> None:
        with pytest.raises(SsrfBlocked):
            async with TinyFishFetchClient(
                api_key="k", base_url="https://f.test", http=_mock_client(handler)
            ) as c:
                await c.render("https://169.254.169.254/latest/meta-data/")

    run(main())
    assert calls["n"] == 0  # SSRF guard runs before the request is built


# ===========================================================================
# L4 SSRF — static half (allowlist) and DNS half (private answers)
# ===========================================================================
def test_ssrf_rejects_metadata_and_non_gov_hosts():
    from services.tinyfish.client import SsrfBlocked, assert_allowed_gov_url

    for url in (
        "https://169.254.169.254/latest/meta-data/",
        "http://pmkisan.gov.in/",          # not https
        "https://pmkisan.gov.in.evil.com/", # suffix trick
        "https://example.com/",
    ):
        with pytest.raises(SsrfBlocked):
            assert_allowed_gov_url(url)
    assert assert_allowed_gov_url(GOV_URL) == GOV_URL


def test_ssrf_dns_half_rejects_private_resolution():
    from services.tinyfish.client import SsrfBlocked, assert_resolves_public

    def resolver(host: str, *a: Any, **k: Any) -> list[Any]:
        return [(2, 1, 6, "", ("169.254.169.254", 80))]

    with pytest.raises(SsrfBlocked):
        assert_resolves_public("https://pmkisan.gov.in/", resolver)

    def public(host: str, *a: Any, **k: Any) -> list[Any]:
        return [(2, 1, 6, "", ("103.21.58.1", 80))]

    assert assert_resolves_public("https://pmkisan.gov.in/", public) == ["103.21.58.1"]


# ===========================================================================
# Stage 4 — content gate (no LLM spend on garbage)
# ===========================================================================
def test_content_gate_rejects_garbage_and_accepts_real_pages():
    from services.tinyfish.parser import content_gate

    # content_gate() strips first, so the filler must be real words:
    # the length gate (MIN_CONTENT_LENGTH = 200) runs before the lexicons.
    pad = "Eligibility details for this scheme. " * 8
    assert content_gate("too short").startswith("content_too_short:")
    assert content_gate(None).startswith("content_too_short:")
    assert content_gate("Site is under maintenance. Back soon." + pad).startswith(
        "maintenance_page:"
    )
    assert content_gate("Just a moment... checking your browser" + pad).startswith(
        "error_shell:"
    )
    assert content_gate("# Scheme\n" + "Eligibility and benefits. " * 20) is None


# ===========================================================================
# The L1–L5 ladder
# ===========================================================================
def _ladder(raw: Any, *, conf: float = 0.9, check_dns: bool = False, resolver=None):
    from services.tinyfish.parser import provenance_from_span, run_ladder

    return run_ladder(
        raw,
        provenance=provenance_from_span("source span"),
        parse_confidence=conf,
        check_dns=check_dns,
        resolver=resolver,
    )


def test_ladder_accepts_the_canonical_seed():
    res = _ladder(seed())
    assert res.ok and res.layer is None and res.scheme is not None


def test_l1_forbids_unknown_keys():
    raw = dict(seed())
    raw["bogus_key"] = 1
    res = _ladder(raw)
    assert not res.ok and res.layer == "L1"
    assert res.tags == ["L1:extra_forbidden:bogus_key"]


def test_l1_reports_missing_required_keys():
    raw = dict(seed())
    raw.pop("department")
    res = _ladder(raw)
    assert res.layer == "L1" and any(t.startswith("L1:missing:") for t in res.tags)


def test_l2_never_coerces_a_string_to_an_int():
    """DATA_SPEC §7.4: `"250000"` for an integer field is a VIOLATION."""
    raw = dict(seed())
    raw["income_ceiling_annual"] = "250000"
    res = _ladder(raw)
    assert not res.ok and res.layer == "L2"
    assert any(t.startswith("L2:int_type:income_ceiling_annual") for t in res.tags), res.tags


def test_l3_cross_field_invariant_vcf1():
    """V-CF1 belongs to L3 (DATA_SPEC §7.4), even though ``Scheme`` also
    re-checks it as a model validator."""
    raw = dict(seed())
    raw["min_age"] = 40
    raw["max_age"] = 25
    res = _ladder(raw)
    assert not res.ok and res.layer == "L3"
    assert res.tags == ["L3:V-CF1:"] or res.tags[0].startswith("L3:V-CF1")
    assert "V-CF1" in res.violations[0].message


def test_l3_cross_field_invariant_vcf4_is_l3_only():
    """V-CF4 has no Pydantic twin, so it must land on L3 unaided."""
    raw = dict(seed())
    raw["intake_window"] = dict(raw["intake_window"])
    raw["intake_window"]["cycle_status"] = "closed"  # INTAKE_OPEN + closed
    res = _ladder(raw)
    assert res.layer == "L3"
    assert any("V-CF4" in t for t in res.tags)


def test_l3_missing_provenance_is_a_failure():
    from services.tinyfish.parser import run_ladder

    res = run_ladder(seed(), provenance={}, parse_confidence=0.9, check_dns=False)
    assert res.layer == "L3"
    assert any(t.startswith("L3:provenance_span_absent:") for t in res.tags)


def test_l4_dns_half_blocks_and_is_marked_security():
    """URL passes the schema pattern, so L2 is green and L4 is what fires."""

    def private(host: str, *a: Any, **k: Any) -> list[Any]:
        return [(2, 1, 6, "", ("10.0.0.7", 80))]

    raw = dict(seed())
    raw["portal_url"] = "https://demo.gov.in/apply"
    res = _ladder(raw, check_dns=True, resolver=private)
    assert res.layer == "L4"
    assert res.tags == ["L4:ssrf_blocked:portal_url"]


def test_l5_confidence_floor_is_frozen_at_075():
    from services.tinyfish.parser import PARSE_CONFIDENCE_FLOOR

    assert PARSE_CONFIDENCE_FLOOR == 0.75  # ARCHITECTURE §9
    assert _ladder(seed(), conf=0.75).ok is True
    res = _ladder(seed(), conf=0.749)
    assert not res.ok and res.layer == "L5"
    assert res.tags[0].startswith("L5:")


# ===========================================================================
# The pipeline — repair x1, then accept | HRQ
# ===========================================================================
def _llm_factory(candidates: list[tuple[dict, float]]) -> Callable[..., Any]:
    """Returns an ``llm=`` stub replaying ``candidates`` (one per attempt)."""
    calls: list[dict] = []

    async def fake_llm(
        markdown: str,
        *,
        previous: Any = None,
        violations: Any = (),
        **kw: Any,
    ) -> Any:
        from services.tinyfish.parser import LLMOutput, provenance_from_span

        idx = min(len(calls), len(candidates) - 1)
        cand, conf = candidates[idx]
        calls.append({"previous": previous, "violations": list(violations)})
        return LLMOutput(cand, provenance_from_span("span"), conf)

    fake_llm.calls = calls  # type: ignore[attr-defined]
    return fake_llm


def _fetch() -> dict:
    return {
        "url": GOV_URL,
        "final_url": GOV_URL,
        "http_status": 200,
        "content_hash": "a" * 64,
        "markdown": "# Pm Kisan " + "eligibility " * 30,
    }


def test_accept_path_commits_mongo_then_calls_indexer(db: FakeDB):
    from services.tinyfish.parser import ingest_markdown

    order: list[str] = []
    llm = _llm_factory([(seed(), 0.95)])

    def indexer(model: Any) -> None:
        order.append("indexer")

    original = db.replace_one

    def tracking_replace(flt, doc, upsert=False):  # type: ignore[no-untyped-def]
        order.append("mongo")
        original(flt, doc, upsert)

    db.replace_one = tracking_replace  # type: ignore[method-assign]

    async def main():
        return await ingest_markdown(_fetch()["markdown"], fetch=_fetch(), db=db,
                                     indexer=indexer, llm=llm)

    out = run(main())
    assert out.result == "ok" and out.index_state == "indexed"
    assert order == ["mongo", "indexer"]  # Phase 1 §3.1 write ordering
    assert db.logs and db.logs[0]["stage"] == "parse"


def test_repair_runs_exactly_once_then_accepts(db: FakeDB):
    from services.tinyfish.parser import ingest_markdown

    broken = dict(seed())
    broken["bogus_key"] = 1
    llm = _llm_factory([(broken, 0.95), (seed(), 0.95)])

    async def main():
        return await ingest_markdown(_fetch()["markdown"], fetch=_fetch(), db=db,
                                     indexer=lambda m: None, llm=llm)

    out = run(main())
    assert out.repaired and out.result == "repaired"
    assert len(llm.calls) == 2  # initial + 1 repair (MAX_REPAIR_ATTEMPTS = 1)
    assert llm.calls[1]["previous"] is not None and llm.calls[1]["violations"]
    assert out.index_state == "indexed"


def test_hrq_low_confidence_never_repairs_and_never_indexes(db: FakeDB):
    from services.tinyfish.parser import ingest_markdown

    llm = _llm_factory([(seed(), 0.4)])  # < 0.75 -> L5
    indexed: list[Any] = []

    async def main():
        return await ingest_markdown(_fetch()["markdown"], fetch=_fetch(), db=db,
                                     indexer=indexed.append, llm=llm)

    out = run(main())
    assert out.result == "human_review" and out.layer == "L5"
    assert out.index_state == "pending_review" and out.repaired is False
    assert len(llm.calls) == 1  # L5 never retries (DATA_SPEC §7.4)
    assert indexed == []  # never embedded, never upserted to Qdrant

    (doc,) = db.documents
    assert doc["index_state"] == "pending_review"
    assert doc["is_active"] is False
    assert doc["parse"]["repair_attempts"] == 0
    assert doc["parse"]["review_reasons"] and doc["parse"]["review_reasons"][0].startswith("L5:")


def test_hrq_l4_ssrf_never_repairs(db: FakeDB):
    from services.tinyfish.parser import ingest_markdown

    poisoned = dict(seed())
    poisoned["portal_url"] = "https://demo.gov.in/apply"
    llm = _llm_factory([(poisoned, 0.95)])
    indexed: list[Any] = []

    def private(host: str, *a: Any, **k: Any) -> list[Any]:
        return [(2, 1, 6, "", ("192.168.1.10", 80))]

    async def main():
        return await ingest_markdown(_fetch()["markdown"], fetch=_fetch(), db=db,
                                     indexer=indexed.append, llm=llm,
                                     check_dns=True, resolver=private)

    out = run(main())
    assert out.layer == "L4" and out.result == "human_review"
    assert len(llm.calls) == 1  # security failures are never auto-retried
    assert indexed == []
    (doc,) = db.documents
    assert doc["index_state"] == "pending_review"


def test_hrq_l1_falls_through_to_the_single_repair(db: FakeDB):
    from services.tinyfish.parser import ingest_markdown

    broken = dict(seed())
    broken["bogus_key"] = "still broken on the repair pass"
    llm = _llm_factory([(broken, 0.95), (broken, 0.95)])
    indexed: list[Any] = []

    async def main():
        return await ingest_markdown(_fetch()["markdown"], fetch=_fetch(), db=db,
                                     indexer=indexed.append, llm=llm)

    out = run(main())
    assert out.layer == "L1" and out.result == "human_review"
    assert out.repaired is True
    assert len(llm.calls) == 2  # one repair attempt, then HRQ
    assert out.index_state == "pending_review" and indexed == []
    (doc,) = db.documents
    assert doc["parse"]["repair_attempts"] == 1
    assert doc["is_active"] is False


def test_content_gate_short_circuits_before_any_llm_call(db: FakeDB):
    from services.tinyfish.parser import ingest_markdown

    calls = {"n": 0}

    async def llm(*a: Any, **k: Any) -> Any:  # pragma: no cover - must not run
        calls["n"] += 1
        raise AssertionError("LLM must not be called for a rejected page")

    async def main():
        return await ingest_markdown("Just a moment... checking your browser",
                                     fetch=_fetch(), db=db, llm=llm)

    out = run(main())
    assert out.result == "rejected_content_gate"
    assert calls["n"] == 0
    assert db.documents == [] and db.logs[0]["result"] == "rejected_content_gate"


def test_llm_unavailable_degrades_to_human_review_not_5xx(db: FakeDB):
    from services.tinyfish.parser import LLMUnavailable, ingest_markdown

    async def llm(*a: Any, **k: Any) -> Any:
        raise LLMUnavailable("LLM_BASE_URL / LLM_API_KEY not configured")

    async def main():
        return await ingest_markdown(_fetch()["markdown"], fetch=_fetch(), db=db, llm=llm)

    out = run(main())
    assert out.result == "human_review"
    assert out.violations and out.violations[0].startswith("L0:llm_unavailable")
    assert out.index_state is None  # nothing was written at all


def test_ingest_without_db_still_runs_the_ladder():
    """Degrading to 'no datastore' must not change the verdict (§8.2)."""
    from services.tinyfish.parser import ingest_markdown

    llm = _llm_factory([(seed(), 0.95)])

    async def main():
        return await ingest_markdown(_fetch()["markdown"], fetch=_fetch(), db=None, llm=llm)

    out = run(main())
    assert out.result == "ok" and out.scheme is not None


# ===========================================================================
# WORKFLOW §2.4 — date normalisation
# ===========================================================================
TODAY = date(2026, 10, 9)


@pytest.mark.parametrize(
    "text,expected,signal",
    [
        ("2026-11-15", date(2026, 11, 15), None),
        ("10/31/2026", date(2026, 10, 31), None),        # month > 12 -> MM/DD
        ("31/10/2026", date(2026, 10, 31), None),        # day   > 12 -> DD/MM
        ("10/11/2026", date(2026, 11, 10), "date_convention_ambiguous"),
        ("15th November 2026", date(2026, 11, 15), None),
        ("November 15, 2026", date(2026, 11, 15), None),
        ("Last date: 31/12/26", date(2026, 12, 31), "date_two_digit_year"),
        ("FY 2026-27", None, "date_rejected_not_a_deadline"),
        ("last date is 2026-13-40", None, "date_rejected_not_a_deadline"),
    ],
)
def test_date_normalisation_table(text, expected, signal):
    from services.tinyfish.agent import normalize_deadline

    assert normalize_deadline(text, TODAY) == (expected, signal)


def test_deadline_selection_precedence():
    """§2.3 step 12: labelled-future > future > labelled-past > past."""
    from services.tinyfish.agent import select_deadline

    got = select_deadline(
        [("Last date 2026-09-01", True), ("session starts 2026-12-01", False)], TODAY
    )
    assert got[:3] == (date(2026, 12, 1), "deadline_from_unlabelled_context", False)

    got = select_deadline([("Last date 2026-12-31", True), ("2026-12-01", False)], TODAY)
    assert got[:3] == (date(2026, 12, 31), "deadline_text_found", True)

    got = select_deadline([("Last date 2026-09-01", True)], TODAY)
    assert got[:3] == (date(2026, 9, 1), "deadline_in_past", True)

    assert select_deadline([], TODAY)[1] == "no_deadline_found"


def test_deadline_selection_surfaces_the_convention_signals():
    """§2.4 signals must reach observed_signals[] even when they lose."""
    from services.tinyfish.agent import select_deadline

    got = select_deadline(
        [("Last date 10/11/2026", True), ("closing 31/12/26", False)], TODAY
    )
    assert got[0] == date(2026, 11, 10)          # DD/MM en-IN default wins
    assert "date_convention_ambiguous" in got[3]
    assert "date_two_digit_year" in got[3]


# ===========================================================================
# WORKFLOW §2.5 — 9-row verdict precedence (first match wins)
# ===========================================================================
def _sig(state: str = "enabled", closed: bool = False, overlay: bool = False) -> dict:
    return {"control_state": state, "closed_language": closed, "overlay": overlay}


@pytest.mark.parametrize(
    "name,signals,deadline,label_bound,expected",
    [
        # Rows 1-5 (WAF / captcha / 5xx / maintenance / overlay) are decided
        # from page signals *before* decide() runs and are covered by the
        # agent tests below; this table is rows 6-9.
        ("r6 closed lang", _sig(closed=True), date(2026, 12, 1), True, "INTAKE_CLOSED"),
        ("r6 label past", _sig(), date(2026, 9, 1), True, "INTAKE_CLOSED"),
        ("r7 open future", _sig(), date(2026, 12, 1), True, "INTAKE_OPEN"),
        ("r7 open no deadline", _sig(), None, False, "INTAKE_OPEN"),
        ("r7 open unlabelled future", _sig(), date(2026, 12, 1), False, "INTAKE_OPEN"),
        ("r8 enabled past", _sig(), date(2026, 9, 1), True, "INTAKE_CLOSED"),
        ("r9 control absent", _sig("absent"), None, False, "UNREACHABLE"),
        ("r9 control disabled", _sig("disabled"), None, False, "UNREACHABLE"),
    ],
)
def test_verdict_precedence_table(name, signals, deadline, label_bound, expected):
    from services.tinyfish.agent import decide

    status, _err, out = decide(signals, deadline, label_bound, TODAY)
    assert status.value == expected, out


def test_verdict_row_9_carries_the_ambiguous_signal():
    from services.tinyfish.agent import SIGNAL_AMBIGUOUS, decide

    status, err, out = decide(_sig("absent"), None, False, TODAY)
    assert status.value == "UNREACHABLE"
    assert err == SIGNAL_AMBIGUOUS and SIGNAL_AMBIGUOUS in out


# ===========================================================================
# Tier 3 — the web agent (injected browser + snapshot store)
# ===========================================================================
class _Resp:
    def __init__(self, status: int = 200) -> None:
        self.status = status


class _Keyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    async def press(self, key: str) -> None:
        self.pressed.append(key)


class FakePage:
    """Replays scripted ``evaluate`` payloads; records screenshot requests."""

    def __init__(self, script: list[Any], *, url: str = GOV_URL, status: int = 200,
                 raise_on_goto: Exception | None = None) -> None:
        self.script = list(script)
        self.url = url
        self.status = status
        self.raise_on_goto = raise_on_goto
        self.keyboard = _Keyboard()
        self.screenshots = 0
        self.goto_calls = 0

    async def goto(self, url: str, wait_until: str | None = None,
                   timeout: int | None = None) -> _Resp:
        self.goto_calls += 1
        if self.raise_on_goto:
            raise self.raise_on_goto
        return _Resp(self.status)

    async def evaluate(self, js: str) -> Any:
        idx = getattr(self, "_idx", 0)
        item = self.script[min(idx, len(self.script) - 1)]
        self._idx = idx + 1  # type: ignore[attr-defined]
        return item

    async def screenshot(self, type: str | None = None) -> bytes:  # noqa: A002
        self.screenshots += 1
        return b"\x89PNG\r\n\x1a\nfake"


def _page_sig(state: str = "enabled", *, closed: bool = False, maintenance: bool = False,
              waf: bool = False, captcha: bool = False, overlay: bool = False,
              deadlines: list[dict] | None = None) -> dict:
    return {
        "final_url": GOV_URL,
        "control_state": state,
        "control_count": 2 if state != "absent" else 0,
        "closed_language": closed,
        "maintenance": maintenance,
        "waf": waf,
        "captcha": captcha,
        "overlay": overlay,
        "deadlines": deadlines or [],
    }


_DISMISS = {"dismissed": 0, "cookie": False}


class FakeSnapshots:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.keys: list[str] = []

    async def put(self, key: str, data: bytes) -> str | None:
        self.keys.append(key)
        return None if self.fail else f"http://minio:9000/{key}"


def _agent(script: list[Any], *, snapshots: FakeSnapshots | None = None,
           goto_exc: Exception | None = None, check_dns: bool = False,
           db: Any | None = None, **page_kw: Any):
    from services.tinyfish.agent import TinyFishWebAgentClient

    page = FakePage(script, raise_on_goto=goto_exc, **page_kw)
    closed: list[bool] = []

    async def factory():
        async def closer() -> None:
            closed.append(True)
        return page, closer

    agent = TinyFishWebAgentClient(
        browser_factory=factory,
        snapshots=snapshots or FakeSnapshots(),
        check_dns=check_dns,
        db=db,
    )
    return agent, page, closed


OPEN_PAGE = [
    _page_sig("enabled", deadlines=[{"t": "Last date 31/12/2026", "label": True}]),
    _DISMISS,
    _page_sig("enabled", deadlines=[{"t": "Last date 31/12/2026", "label": True}]),
]


def test_agent_intake_open_and_final_stream_yield_is_the_enum():
    agent, page, closed = _agent(OPEN_PAGE)

    async def main():
        res = await agent.verify(SCH_ID, GOV_URL, "job-1")
        items = [i async for i in agent.verify_stream(SCH_ID, GOV_URL, "job-2")]
        return res, items

    res, items = run(main())
    assert res.verification_status.value == "INTAKE_OPEN"
    assert res.extracted_deadline == date(2026, 12, 31)
    assert res.snapshot_url and res.snapshot_url.endswith(f"snapshots/{SCH_ID}/job-1.png")
    # one screenshot per run: verify() and verify_stream() both execute
    assert page.screenshots == 2 and closed  # browser always closed
    assert items[-1] == "INTAKE_OPEN"  # final yield = strict enum value
    assert "snapshot_captured" in res.observed_signals
    assert "landing_page_classified" in res.observed_signals


def test_agent_intake_closed_on_closed_language():
    script = [
        _page_sig("enabled", closed=True, deadlines=[{"t": "Last date 01/09/2026", "label": True}]),
        _DISMISS,
        _page_sig("enabled", closed=True, deadlines=[{"t": "Last date 01/09/2026", "label": True}]),
    ]
    agent, _page, _closed = _agent(script)
    res = run(agent.verify(SCH_ID, GOV_URL, "job-closed"))
    assert res.verification_status.value == "INTAKE_CLOSED"
    assert "closed_language_present" in res.observed_signals


def test_agent_unreachable_when_navigation_fails():
    agent, page, closed = _agent([], goto_exc=TimeoutError("nav timeout"))
    res = run(agent.verify(SCH_ID, GOV_URL, "job-timeout"))
    assert res.verification_status.value == "UNREACHABLE"
    assert res.error_class == "timeout"
    assert res.snapshot_url is None  # §2.7 C3: no landing page, no evidence
    assert closed  # the browser context is still torn down


def test_agent_unreachable_on_5xx_landing_page():
    agent, _page, _closed = _agent([_page_sig()], status=503)
    res = run(agent.verify(SCH_ID, GOV_URL, "job-5xx"))
    assert res.verification_status.value == "UNREACHABLE"
    assert res.error_class == "http_5xx"


def test_agent_blocked_on_waf_shell():
    agent, _page, _closed = _agent([_page_sig("absent", waf=True)])
    res = run(agent.verify(SCH_ID, GOV_URL, "job-waf"))
    assert res.verification_status.value == "BLOCKED"
    assert res.error_class == "waf_block"


def test_agent_blocked_on_captcha_when_control_unreachable():
    agent, _page, _closed = _agent([_page_sig("absent", captcha=True)])
    res = run(agent.verify(SCH_ID, GOV_URL, "job-captcha"))
    assert res.verification_status.value == "BLOCKED"
    assert res.error_class == "captcha"


def test_agent_ssrf_preflight_blocks_before_the_browser_opens():
    from services.tinyfish.agent import TinyFishWebAgentClient

    opened = {"n": 0}

    async def factory():
        opened["n"] += 1
        raise AssertionError("browser must not open for a blocked URL")

    agent = TinyFishWebAgentClient(browser_factory=factory, check_dns=False)
    res = run(agent.verify(SCH_ID, "https://169.254.169.254/latest/meta-data/", "job-ssrf"))
    assert res.verification_status.value == "UNREACHABLE"
    assert res.error_class == "ssrf_blocked"
    assert opened["n"] == 0


def test_agent_snapshot_failure_does_not_change_the_verdict():
    snaps = FakeSnapshots(fail=True)
    agent, _page, _closed = _agent(OPEN_PAGE, snapshots=snaps)
    res = run(agent.verify(SCH_ID, GOV_URL, "job-nosnap"))
    assert res.verification_status.value == "INTAKE_OPEN"  # evidence is not the input
    assert res.snapshot_url is None
    assert "snapshot_failed" in res.observed_signals


def test_agent_ambiguous_when_control_absent_and_no_deadline():
    script = [_page_sig("absent"), _DISMISS, _page_sig("absent")]
    agent, _page, _closed = _agent(script)
    res = run(agent.verify(SCH_ID, GOV_URL, "job-amb"))
    assert res.verification_status.value == "UNREACHABLE"
    assert "verdict_ambiguous" in res.observed_signals


def test_agent_always_closes_the_browser_even_if_the_consumer_walks_away():
    agent, _page, closed = _agent(OPEN_PAGE)

    async def main() -> None:
        stream = agent.verify_stream(SCH_ID, GOV_URL, "job-abandon")
        async for frame in stream:
            if frame == "navigating":  # browser is open by now
                break

    run(main())
    assert closed, "a consumer that stops reading must not leak a Playwright context"


# ===========================================================================
# §2.3 steps 15-16 — verification_logs + the verification_* cluster
# (BUILD_ORDER task 3.10, DATA_SPEC §8.1 and §6)
# ===========================================================================
def test_step15_inserts_exactly_one_verification_logs_row_per_run(db: FakeDB):
    """§2.3 step 15: ALWAYS a verification_logs row — this is the only durable
    record that the run happened, so it carries the whole §8.1 field set."""
    agent, _page, _closed = _agent(OPEN_PAGE, db=db)
    res = run(agent.verify(SCH_ID, GOV_URL, "job-audit"))

    assert len(db.verification_rows) == 1, db.verification_rows
    row = db.verification_rows[0]
    assert row["verification_job_id"] == "job-audit"
    assert row["scheme_id"] == SCH_ID
    assert row["portal_url"] == GOV_URL
    assert row["final_url"] == GOV_URL
    assert row["verification_status"] == "INTAKE_OPEN"
    assert row["extracted_deadline"] == "2026-12-31"      # ISO-8601, not a date
    assert row["snapshot_key"] == f"snapshots/{SCH_ID}/job-audit.png"
    assert row["duration_ms"] == res.duration_ms <= 6000
    assert row["agent_version"] and row["error_class"] is None
    assert row["started_at"] <= row["finished_at"]
    assert "submit_control_enabled" in row["observed_signals"]
    # the audit row is written even when db is not used for the ingest path
    assert not [e for e in db.events], "must not also touch ingestion collections"


def test_step15_runs_on_a_failed_verification_too(db: FakeDB):
    """No silent success (WORKFLOW §3.2): a failed run still leaves a row."""
    agent, _page, _closed = _agent([], goto_exc=TimeoutError("nav timeout"), db=db)
    res = run(agent.verify(SCH_ID, GOV_URL, "job-fail"))
    assert res.error_class == "timeout"
    assert len(db.verification_rows) == 1
    row = db.verification_rows[0]
    assert row["verification_status"] == "UNREACHABLE"
    assert row["error_class"] == "timeout"
    assert row["snapshot_key"] is None


def test_step16_success_stamps_verified_at_and_clears_last_known(db: FakeDB):
    """DATA_SPEC §6 'Open' row: fresh verified_at, job id set, last_known null."""
    db.prior = {
        "scheme_id": SCH_ID,
        "verification_status": "UNREACHABLE",
        "verified_at": None,
        "extracted_deadline": None,
        "last_known_status": "INTAKE_OPEN",
    }
    agent, _page, _closed = _agent(OPEN_PAGE, db=db)
    res = run(agent.verify(SCH_ID, GOV_URL, "job-ok"))

    assert len(db.updates) == 1
    assert db.updates[0]["filter"] == {"scheme_id": SCH_ID}
    patch = db.updates[0]["update"]["$set"]
    assert patch["verification_status"] == "INTAKE_OPEN"
    assert isinstance(patch["verified_at"], datetime)      # server-generated
    assert patch["verified_at"] > datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert patch["extracted_deadline"] == "2026-12-31"
    assert patch["snapshot_url"] == res.snapshot_url
    assert patch["verification_job_id"] == "job-ok"
    assert patch["last_known_status"] is None


def test_step16_failure_preserves_verified_at_and_records_last_known(db: FakeDB):
    """DATA_SPEC §6 'Outage' row — verified_at is preserved so is_stale keeps
    ageing honestly; the failure never overwrites the last good deadline."""
    prior_instant = datetime(2026, 10, 6, 18, 10, tzinfo=timezone.utc)
    db.prior = {
        "scheme_id": SCH_ID,
        "verification_status": "INTAKE_OPEN",
        "verified_at": prior_instant,
        "extracted_deadline": "2026-11-30",
        "last_known_status": None,
    }
    agent, _page, _closed = _agent([], goto_exc=TimeoutError("nav timeout"), db=db)
    res = run(agent.verify(SCH_ID, GOV_URL, "job-down"))
    assert res.verification_status.value == "UNREACHABLE"

    patch = db.updates[-1]["update"]["$set"]
    assert patch["verification_status"] == "UNREACHABLE"
    assert patch["verified_at"] == prior_instant           # preserved, not reset
    assert patch["extracted_deadline"] == "2026-11-30"     # preserved
    assert patch["snapshot_url"] is None
    assert patch["verification_job_id"] is None            # lives in the log only
    assert patch["last_known_status"] == "INTAKE_OPEN"


def test_persistence_failure_never_changes_the_verdict():
    """Persistence is evidence, never the decision: Mongo down => still a verdict."""

    class _BrokenDB:
        def __init__(self) -> None:
            self.verification_logs = self
            self.schemes = self

        def insert_one(self, doc: dict) -> None:
            raise RuntimeError("mongo down")

        def find_one(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("mongo down")

        def update_one(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("mongo down")

    agent, _page, _closed = _agent(OPEN_PAGE, db=_BrokenDB())
    res = run(agent.verify(SCH_ID, GOV_URL, "job-no-mongo"))
    assert res.verification_status.value == "INTAKE_OPEN"
    assert res.snapshot_url


# ===========================================================================
# BUILD_ORDER §5.4 — Tier 3 must be disableable without collateral damage
# ===========================================================================
def test_disabling_tier3_leaves_the_ingestion_path_unaffected(db: FakeDB, monkeypatch):
    """Tier 3 is a *verification* service; ingestion (Tier 1/2 -> ladder -> HRQ)
    must keep working when the browser agent cannot even be constructed."""
    import services.tinyfish.agent as agent_mod
    from services.tinyfish.parser import ingest_markdown

    def _disabled(*a: Any, **k: Any) -> Any:
        raise AssertionError("ingestion must never construct a Tier-3 agent")

    monkeypatch.setattr(agent_mod, "TinyFishWebAgentClient", _disabled)

    llm = _llm_factory([(seed(), 0.95)])

    async def main():
        return await ingest_markdown(
            _fetch()["markdown"], fetch=_fetch(), db=db, llm=llm
        )

    out = run(main())
    assert out.result == "ok" and out.index_state == "indexed"
    assert db.logs and db.logs[0]["stage"] == "parse"


# ===========================================================================
# DATA_SPEC §7.5 row 4 — enum invented outside the whitelist
# ===========================================================================
def test_l2_rejects_an_enum_outside_the_whitelist():
    """`"category": "education-scholarship"` is not a category -> L2, one
    repair attempt, then Human Review Queue.  Never coerced, never indexed."""
    raw = dict(seed())
    raw["category"] = "education-scholarship"
    res = _ladder(raw)
    assert not res.ok and res.layer == "L2"
    assert any("category" in t for t in res.tags), res.tags
    assert res.scheme is None


# ===========================================================================
# §2.7 TC-C8 — the hard 6,000 ms Tier-3 budget (Phase 1 §7.3)
# ===========================================================================
def test_tc_c8_over_budget_job_completes_as_unreachable_timeout(caplog):
    """TC-C8: agent exceeds the Tier-3 budget -> job completes, status
    UNREACHABLE, error_class=timeout, duration_ms over budget, logged as
    over-budget, and nothing raises (the citizen response is never blocked)."""
    import logging as _logging

    from services.tinyfish.agent import TinyFishWebAgentClient

    class _SlowPage(FakePage):
        async def goto(self, url: str, wait_until: str | None = None,
                       timeout: int | None = None) -> _Resp:
            self.goto_calls += 1
            await asyncio.sleep(0.12)   # blows the 10 ms budget below
            return _Resp(200)

    page = _SlowPage(OPEN_PAGE)
    closed: list[bool] = []

    async def factory():
        async def closer() -> None:
            closed.append(True)
        return page, closer

    agent = TinyFishWebAgentClient(
        browser_factory=factory, snapshots=FakeSnapshots(), timeout_ms=10
    )

    with caplog.at_level(_logging.WARNING, logger="services.tinyfish.agent"):
        res = run(agent.verify(SCH_ID, GOV_URL, "job-tcc8"))

    assert res.duration_ms > 10                      # over budget, and reported so
    assert res.verification_status.value == "UNREACHABLE"
    assert res.error_class == "timeout"
    assert res.snapshot_url is None                  # budget spent before step 13
    assert closed                                     # context closed => job completed
    assert "over budget" in caplog.text
    # "timeout" is an error_class, never an observed_signals[] value
    assert not set(res.observed_signals) - SIGNAL_CATALOG, res.observed_signals


def test_under_budget_success_is_not_rewritten_by_the_budget_gate():
    """The gate must not fire on a healthy run — INTAKE_OPEN survives."""
    agent, _page, _closed = _agent(OPEN_PAGE)
    res = run(agent.verify(SCH_ID, GOV_URL, "job-under-budget"))
    assert res.verification_status.value == "INTAKE_OPEN"
    assert res.error_class is None
    assert res.duration_ms <= agent.timeout_ms


# ===========================================================================
# Snapshot writer — WORKFLOW §2.3 step 13 (SigV4 against MinIO, mocked here)
# ===========================================================================
def test_snapshot_store_ensures_bucket_and_lifecycle_before_the_first_write():
    """Build Order 3.9: bucket + the 90-day rule are installed once, up front."""
    from services.tinyfish.agent import SnapshotStore

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.raw_path.decode())
        return httpx.Response(200, content=b"")

    store = SnapshotStore(
        endpoint="http://minio.test",
        bucket="schemeradar-snapshots",
        access_key="AKIA_TEST",
        secret_key="secret",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert run(store.put("snapshots/sch_1/job_1.png", b"\x89PNG-bytes")) is not None
    assert calls == [
        "/schemeradar-snapshots",              # CreateBucket (idempotent)
        "/schemeradar-snapshots?lifecycle=",   # 90-day rule on snapshots/
        "/schemeradar-snapshots/snapshots/sch_1/job_1.png",
    ]

    # Same instance, second write: neither setup call may be repeated.
    calls.clear()
    assert run(store.put("snapshots/sch_1/job_2.png", b"b")) is not None
    assert calls == ["/schemeradar-snapshots/snapshots/sch_1/job_2.png"]


def test_snapshot_store_installs_a_90_day_expiry_rule_scoped_to_snapshots_prefix():
    import base64
    import hashlib

    from services.tinyfish.agent import SnapshotStore

    lifecycle: list[tuple[str, bytes, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if b"lifecycle" in request.url.query:
            lifecycle.append((
                request.url.raw_path.decode(), request.content, dict(request.headers),
            ))
        return httpx.Response(200, content=b"")

    store = SnapshotStore(
        endpoint="http://minio.test",
        bucket="schemeradar-snapshots",
        access_key="AKIA_TEST",
        secret_key="secret",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    run(store.put("snapshots/sch_1/job_1.png", b"\x89PNG-bytes"))

    assert len(lifecycle) == 1                       # exactly one rule, once
    path, xml, headers = lifecycle[0]
    assert path == "/schemeradar-snapshots?lifecycle="
    assert headers.get("content-type") == "application/xml"
    body = xml.decode("utf-8")
    assert "<Expiration><Days>90</Days></Expiration>" in body
    assert "<Prefix>snapshots/</Prefix>" in body     # scoped — nothing else expires
    assert "<Status>Enabled</Status>" in body
    # Live MinIO rejects this operation without an integrity header
    # (`MissingContentMD5`), and it must be covered by the signature too.
    assert headers.get("content-md5")
    assert "content-md5" in headers["authorization"].lower()
    signed = headers["authorization"].split("SignedHeaders=")[1].split(",")[0]
    assert "content-md5" in signed
    assert headers["content-md5"] == base64.b64encode(
        hashlib.md5(xml, usedforsecurity=False).digest()
    ).decode("ascii")


def test_snapshot_store_self_creates_the_bucket_and_retries_the_object_once():
    """404 on the object PUT -> CreateBucket -> PUT retried once (belt & braces)."""
    from services.tinyfish.agent import SnapshotStore

    calls: list[str] = []
    object_puts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        raw = request.url.raw_path.decode()
        calls.append(raw)
        if "lifecycle" in raw or raw == "/schemeradar-snapshots":
            return httpx.Response(200, content=b"")
        object_puts["n"] += 1
        if object_puts["n"] == 1:
            return httpx.Response(404, content=b"<Error><Code>NoSuchBucket</Code></Error>")
        return httpx.Response(200, content=b"")

    store = SnapshotStore(
        endpoint="http://minio.test",
        bucket="schemeradar-snapshots",
        access_key="AKIA_TEST",
        secret_key="secret",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    url = run(store.put("snapshots/sch_1/job_1.png", b"\x89PNG-bytes"))

    assert url == "http://minio.test/schemeradar-snapshots/snapshots/sch_1/job_1.png"
    assert calls[2:] == [
        "/schemeradar-snapshots/snapshots/sch_1/job_1.png",   # 404
        "/schemeradar-snapshots",                              # CreateBucket
        "/schemeradar-snapshots/snapshots/sch_1/job_1.png",    # retried once
    ]


def test_generate_snapshot_url_mints_a_presigned_get():
    """§7.3 signed read: query-string auth, one hour, never a plain link."""
    from urllib.parse import parse_qsl, urlsplit

    from services.tinyfish.agent import SNAPSHOT_URL_TTL_SECONDS, SnapshotStore

    store = SnapshotStore(
        endpoint="http://minio.test:9000", bucket="b",
        access_key="AKIA_TEST", secret_key="secret",
    )
    url = store.generate_snapshot_url("snapshots/sch_1/job_1.png", expires_in=600)

    assert url is not None
    split = urlsplit(url)
    assert (split.scheme, split.netloc) == ("http", "minio.test:9000")
    assert split.path == "/b/snapshots/sch_1/job_1.png"

    q = dict(parse_qsl(split.query))
    assert q["X-Amz-Algorithm"] == "AWS4-HMAC-SHA256"
    assert q["X-Amz-Credential"].startswith("AKIA_TEST/")
    assert q["X-Amz-Credential"].endswith("/us-east-1/s3/aws4_request")
    assert q["X-Amz-Expires"] == "600"
    assert q["X-Amz-SignedHeaders"] == "host"
    assert len(q["X-Amz-Signature"]) == 64
    # default TTL is the documented one, and the signature is what varies
    default = store.generate_snapshot_url("snapshots/sch_1/job_1.png")
    assert default is not None and dict(parse_qsl(urlsplit(default).query))[
        "X-Amz-Expires"] == str(SNAPSHOT_URL_TTL_SECONDS)
    # the canonical query must be sorted, or the signature will not match
    assert split.query.split("&X-Amz-Signature=")[0] == "&".join(
        sorted(split.query.split("&X-Amz-Signature=")[0].split("&"))
    )


def test_generate_snapshot_url_accepts_a_stored_key_or_a_full_object_url():
    from urllib.parse import urlsplit

    from services.tinyfish.agent import SnapshotStore

    store = SnapshotStore(
        endpoint="http://minio.test:9000", bucket="b",
        access_key="AK", secret_key="SK",
    )
    stored_url = "http://minio.test:9000/b/snapshots/sch_1/job_1.png"
    from_key = store.generate_snapshot_url("snapshots/sch_1/job_1.png")
    from_url = store.generate_snapshot_url(stored_url)

    assert from_key is not None and from_url is not None
    # A durable URL is signed verbatim — never re-pointed at another host.
    assert urlsplit(from_url).path == "/b/snapshots/sch_1/job_1.png"
    assert urlsplit(from_key).path == "/b/snapshots/sch_1/job_1.png"
    assert urlsplit(from_key).netloc == urlsplit(from_url).netloc


def test_generate_snapshot_url_fails_closed_without_credentials():
    from services.tinyfish.agent import SnapshotStore

    store = SnapshotStore(
        endpoint="http://minio.test", bucket="b", access_key="", secret_key="",
    )
    assert store.generate_snapshot_url("snapshots/sch_1/job_1.png") is None
    assert store.generate_snapshot_url("") is None
    assert store.generate_snapshot_url("http://minio.test") is None


def test_snapshot_store_signs_the_request_and_honours_credentials():
    from services.tinyfish.agent import SnapshotStore

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, content=b"")

    store = SnapshotStore(
        endpoint="http://minio.test", bucket="b",
        access_key="AKIA_TEST", secret_key="secret",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert run(store.put("k.png", b"x")) is not None
    assert seen["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIA_TEST/")
    assert seen["x-amz-content-sha256"]
    assert "signature=" in seen["authorization"].lower()


def test_snapshot_store_fails_closed_without_credentials_and_never_raises():
    from services.tinyfish.agent import SnapshotStore

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network is down")

    store = SnapshotStore(
        endpoint="http://minio.test", bucket="b",
        access_key="", secret_key="",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert run(store.put("k.png", b"x")) is None  # no creds -> no attempt

    store2 = SnapshotStore(
        endpoint="http://minio.test", bucket="b",
        access_key="AK", secret_key="SK",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert run(store2.put("k.png", b"x")) is None  # transport error -> None, no raise


# ===========================================================================
# WORKFLOW §2.5 signals catalog + §2.7 in-job retry
# ===========================================================================
# The 18 values allowed on observed_signals[] (WORKFLOW §2.5).
SIGNAL_CATALOG = {
    "landing_page_classified", "http_status", "notice_popup_dismissed",
    "cookie_banner_dismissed", "submit_control_enabled",
    "submit_control_disabled", "submit_control_absent", "control_count",
    "closed_language_present", "deadline_text_found",
    "deadline_from_unlabelled_context", "no_deadline_found",
    "date_convention_ambiguous", "date_two_digit_year", "deadline_in_past",
    "snapshot_captured", "snapshot_failed", "verdict_ambiguous",
}


def test_observed_signals_stay_inside_the_frozen_catalog():
    """error_class values (timeout, waf_block, ...) are NOT signals."""
    agent, _page, _closed = _agent(OPEN_PAGE)
    res = run(agent.verify(SCH_ID, GOV_URL, "job-cat"))
    unknown = set(res.observed_signals) - SIGNAL_CATALOG
    assert not unknown, f"signals outside the §2.5 catalog: {sorted(unknown)}"


def test_observed_signals_on_the_failure_path_stay_in_catalog():
    agent, _page, _closed = _agent([], goto_exc=TimeoutError("nav timeout"))
    res = run(agent.verify(SCH_ID, GOV_URL, "job-cat-err"))
    assert res.error_class == "timeout"
    assert not set(res.observed_signals) - SIGNAL_CATALOG, res.observed_signals


def test_c3_in_job_retry_after_a_navigation_timeout():
    """§2.7 MAX_AGENT_RETRIES = 1 for timeout/http_5xx while elapsed < 2,000 ms."""
    from services.tinyfish.agent import MAX_AGENT_RETRIES, RETRY_ELIGIBLE_MS

    assert MAX_AGENT_RETRIES == 1 and RETRY_ELIGIBLE_MS == 2000

    attempts = {"n": 0}

    class _FlakyPage(FakePage):
        async def goto(self, url, wait_until=None, timeout=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise TimeoutError("first load timed out")
            return _Resp(200)

    from services.tinyfish.agent import TinyFishWebAgentClient

    page = _FlakyPage([OPEN_PAGE[0], _DISMISS, OPEN_PAGE[2]])
    closed: list[bool] = []

    async def factory():
        async def closer() -> None:
            closed.append(True)
        return page, closer

    agent = TinyFishWebAgentClient(
        browser_factory=factory, snapshots=FakeSnapshots(), check_dns=False
    )
    res = run(agent.verify(SCH_ID, GOV_URL, "job-retry"))
    assert attempts["n"] == 2                       # one in-job retry
    assert res.verification_status.value == "INTAKE_OPEN"  # retry recovered
    assert closed


def test_c3_gives_up_after_the_single_retry():
    attempts = {"n": 0}

    class _DeadPage(FakePage):
        async def goto(self, url, wait_until=None, timeout=None):
            attempts["n"] += 1
            raise TimeoutError("still down")

    from services.tinyfish.agent import TinyFishWebAgentClient

    page = _DeadPage([])
    closed: list[bool] = []

    async def factory():
        async def closer() -> None:
            closed.append(True)
        return page, closer

    agent = TinyFishWebAgentClient(
        browser_factory=factory, snapshots=FakeSnapshots(), check_dns=False
    )
    res = run(agent.verify(SCH_ID, GOV_URL, "job-dead"))
    assert attempts["n"] == 2                       # initial + 1 retry, then stop
    assert res.verification_status.value == "UNREACHABLE"
    assert res.error_class == "timeout"
    assert closed


# ===========================================================================
# Routes — SSE stream + admin ingestion trigger
# ===========================================================================
class StubAgent:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    async def events(self, scheme_id: str, portal_url: str, job_id: str):
        for item in self._items:
            yield item


def _client():
    from starlette.testclient import TestClient

    from services.api.main import app

    return TestClient(app)


def test_verify_stream_emits_sse_frames_and_finishes_on_the_verdict(monkeypatch):
    from services.api import routes
    from services.api.models import VerificationStatus
    from services.api.routes import verify as verify_mod
    from services.tinyfish.agent import VerificationResult

    result = VerificationResult(
        verification_status=VerificationStatus.INTAKE_OPEN,
        extracted_deadline=date(2026, 12, 31),
        snapshot_url="http://minio/snapshots/x.png",
        final_url=GOV_URL,
        observed_signals=["landing_page_classified"],
        duration_ms=12,
        job_id="job",
    )
    monkeypatch.setattr(verify_mod, "portal_url_for", lambda sid: GOV_URL)
    monkeypatch.setattr(
        verify_mod, "make_agent",
        lambda **_kw: StubAgent(["ssrf_preflight", "navigating", result]),
    )
    monkeypatch.setattr(verify_mod, "get_db", lambda: FakeDB())
    _ = routes

    resp = _client().get(f"/api/v1/verify/stream/{SCH_ID}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]
    assert [f["status"] for f in frames] == ["ssrf_preflight", "navigating", "INTAKE_OPEN"]
    assert frames[-1]["done"] is True
    assert frames[-1]["result"]["extracted_deadline"] == "2026-12-31"
    assert frames[-1]["scheme_id"] == SCH_ID
    assert all(f["done"] is False for f in frames[:-1])


def test_verify_stream_issues_the_signed_link_and_never_the_bare_one(monkeypatch):
    """Build Order 3.9: the browser gets an expiring credential, Mongo the key.

    The agent persists *before* this frame exists, so what the API returns can
    be short-lived while what is stored stays durable.
    """
    from services.api.models import VerificationStatus
    from services.api.routes import verify as verify_mod
    from services.tinyfish.agent import VerificationResult

    durable = "http://minio:9000/schemeradar-snapshots/snapshots/sch_1/job_1.png"
    result = VerificationResult(
        verification_status=VerificationStatus.INTAKE_OPEN,
        extracted_deadline=None,
        snapshot_url=durable,
        final_url=GOV_URL,
        observed_signals=["snapshot_captured"],
        duration_ms=12,
        job_id="job-signed",
    )
    seen: list[str | None] = []

    def _sign(url: str | None, *, expires_in: int = 3600) -> str | None:
        seen.append(url)
        return f"{url}?X-Amz-Expires={expires_in}&X-Amz-Signature=deadbeef"

    monkeypatch.setattr(verify_mod, "portal_url_for", lambda sid: GOV_URL)
    monkeypatch.setattr(
        verify_mod, "make_agent",
        lambda **_kw: StubAgent(["capturing_snapshot", result]),
    )
    monkeypatch.setattr(verify_mod, "get_db", lambda: FakeDB())
    monkeypatch.setattr(verify_mod, "sign_snapshot_url", _sign)

    resp = _client().get(f"/api/v1/verify/stream/{SCH_ID}")
    frames = [json.loads(l[6:]) for l in resp.text.splitlines() if l.startswith("data: ")]
    issued = frames[-1]["result"]["snapshot_url"]

    assert seen == [durable]                 # called once, with the durable ref
    assert issued != durable                 # the bare link is never handed out
    assert "X-Amz-Expires=3600" in issued and "X-Amz-Signature=" in issued


def test_sign_snapshot_url_falls_back_to_the_durable_reference(monkeypatch):
    """A signing failure must never drop the evidence from the response."""
    from services.api.routes import verify as verify_mod
    from services.tinyfish import agent as agent_mod

    durable = "http://minio:9000/b/snapshots/sch_1/job_1.png"

    assert verify_mod.sign_snapshot_url(None) is None
    assert verify_mod.sign_snapshot_url("") is None

    # Signing unavailable (no credentials configured) -> pass the reference through.
    monkeypatch.setattr(
        agent_mod.SnapshotStore, "generate_snapshot_url", lambda *_a, **_k: None
    )
    assert verify_mod.sign_snapshot_url(durable) == durable

    # Signing blows up -> still pass it through, never raise into the SSE loop.
    def _boom(*_a, **_k):
        raise RuntimeError("signer is down")

    monkeypatch.setattr(agent_mod.SnapshotStore, "generate_snapshot_url", _boom)
    assert verify_mod.sign_snapshot_url(durable) == durable


def test_verify_stream_404s_for_an_unknown_scheme(monkeypatch):
    from fastapi import HTTPException

    from services.api.routes import verify as verify_mod

    def boom(scheme_id: str) -> str:
        raise HTTPException(status_code=404, detail=f"unknown scheme_id: {scheme_id}")

    monkeypatch.setattr(verify_mod, "portal_url_for", boom)
    assert _client().get(f"/api/v1/verify/stream/nope").status_code == 404


@pytest.fixture
def admin_token():
    from services.api.config import get_settings

    s = get_settings()
    old = s.admin_service_token
    s.admin_service_token = "test-admin-token"
    yield "test-admin-token"
    s.admin_service_token = old


@pytest.fixture
def no_admin_token():
    from services.api.config import get_settings

    s = get_settings()
    old = s.admin_service_token
    s.admin_service_token = ""
    yield
    s.admin_service_token = old


def test_admin_refresh_is_fail_closed_when_no_token_is_configured(no_admin_token):
    resp = _client().post("/api/v1/admin/ingest/refresh", json={"query": "pm kisan"})
    assert resp.status_code == 401
    assert "ADMIN_SERVICE_TOKEN" in resp.json()["detail"]  # actionable, not a bare 401


def test_admin_refresh_rejects_a_wrong_token(admin_token):
    resp = _client().post(
        "/api/v1/admin/ingest/refresh",
        json={"query": "pm kisan"},
        headers={"X-Admin-Token": "wrong"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid admin token"


def test_admin_refresh_accepts_a_valid_token_and_queues_the_job(admin_token):
    resp = _client().post(
        "/api/v1/admin/ingest/refresh",
        json={"query": "pm kisan", "max_results": 3},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted" and body["query"] == "pm kisan"
    assert body["max_results"] == 3 and len(body["job_id"]) == 32

    from services.api.routes import admin as admin_mod

    report = admin_mod.JOBS[body["job_id"]]
    assert report["status"] in {"running", "completed"}
    assert report["stage"] in {"queued", "search", "fetch+parse", "done"}

    # job read-back is authenticated too
    assert _client().get(
        f"/api/v1/admin/ingest/jobs/{body['job_id']}", headers={"X-Admin-Token": admin_token}
    ).status_code == 200
    assert _client().get(f"/api/v1/admin/ingest/jobs/{body['job_id']}").status_code == 401
    admin_mod.JOBS.pop(body["job_id"], None)


def test_admin_refresh_pipeline_runs_search_fetch_parse():
    """The whole Tier 1 → 2 → Parse chain, with every collaborator faked."""
    from services.api.routes import admin as admin_mod
    from services.tinyfish.parser import IngestOutcome

    seen: dict[str, Any] = {}

    from services.tinyfish.client import FetchResult, SearchHit

    class _Search:
        async def discover(self, queries, recency_window=None, max_results=20):
            seen["queries"] = list(queries)
            seen["max_results"] = max_results
            # the Tier-1 allowlist has already run (unit-tested above)
            return [SearchHit(url="https://pmkisan.gov.in/"),
                    SearchHit(url="https://services.punjab.gov.in/y")]

    class _Fetch:
        async def render(self, url, timeout_ms=None, wait_for=None):
            seen.setdefault("fetched", []).append(url)
            return FetchResult(markdown="# ok " * 50, final_url=url,
                               http_status=200, content_hash="b" * 64, url=url)

    db = FakeDB()

    async def fake_ingest(markdown, *, fetch=None, db=None, indexer=None, **kw):
        seen["parsed"] = seen.get("parsed", 0) + 1
        seen["fetch_arg_is_result"] = isinstance(fetch, dict)
        return IngestOutcome("ok", scheme_id=SCH_ID, index_state="indexed")

    monkey = {
        "search_client_factory": admin_mod.search_client_factory,
        "fetch_client_factory": admin_mod.fetch_client_factory,
        "run_ingest": admin_mod.run_ingest,
        "get_db": admin_mod.get_db,
        "indexer_factory": admin_mod.indexer_factory,
    }
    admin_mod.search_client_factory = _Search
    admin_mod.fetch_client_factory = _Fetch
    admin_mod.run_ingest = fake_ingest
    admin_mod.get_db = lambda: db
    admin_mod.indexer_factory = lambda: (lambda model: None)
    admin_mod.JOBS["job-xyz"] = {"status": "running", "stage": "queued",
                                 "query": "pm kisan", "max_results": 5,
                                 "hits": [], "outcomes": []}
    try:
        run(admin_mod.refresh_pipeline("job-xyz", "pm kisan", 5))
    finally:
        for k, v in monkey.items():
            setattr(admin_mod, k, v)

    report = admin_mod.JOBS.setdefault("job-xyz", {})
    assert seen["queries"] == ["pm kisan"] and seen["max_results"] == 5
    assert seen["fetched"] == ["https://pmkisan.gov.in/",
                               "https://services.punjab.gov.in/y"]
    assert seen["parsed"] == 2 and seen["fetch_arg_is_result"] is True
    assert report["status"] == "completed" and report["stage"] == "done"
    admin_mod.JOBS.pop("job-xyz", None)


def test_openapi_publishes_all_step3_routes():
    paths = _client().get("/openapi.json").json()["paths"]
    assert "/api/v1/verify/stream/{scheme_id}" in paths
    assert "/api/v1/admin/ingest/refresh" in paths
    assert "/api/v1/profile/qualify" in paths
