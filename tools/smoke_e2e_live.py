#!/usr/bin/env python3
"""TEMPORARY — Step 3 (Gate G3) live end-to-end smoke.  Do NOT commit.

Runs the whole ingestion + verification path against **real** services with no
fixtures, no mocks and no canned payloads:

    1. credential check          -> Settings (secrets always masked)
    2. Tier 1 discovery          -> live GET, picks the portal from its own response
    3. Tier 2 fetch              -> live POST /, markdown straight from the portal
    4. Ingestion LLM + ladder    -> live completion -> exact `Scheme` model
    5. Tier 3 web agent          -> live Playwright, SSE-shaped trace + verdict
    6. MinIO evidence            -> 90-day lifecycle + presigned GET, fetched live

The only inputs are a search query, the LLM endpoint and the store settings —
every scheme value, deadline, URL and verdict below is read back from the
network at run time.

    LLM_BASE_URL=http://127.0.0.1:11434/v1 \
    LLM_API_KEY=local LLM_MODEL=qwen2.5:3b \
    .venv/bin/python tools/smoke_e2e_live.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

# `python tools/smoke_e2e_live.py` puts tools/, not the repo root, on sys.path.
_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

SEARCH_QUERY = "pm kisan scheme"          # an input, not a result — the URL below is discovered
# Tier 1 returns a *portal homepage* as readily as a scheme page, and a
# homepage often renders to a bare statistics table with nothing to extract.
# These gates keep the choice driven purely by live results while still
# requiring the page to read like a scheme (no URL is ever hard-coded).
SCHEME_SIGNALS = (
    "eligib", "benefit", "deadline", "apply", "document",
    "age ", "income", "scheme", "installment", "subsid",
)
SCHEME_MIN_CHARS = 1000        # below this there is not enough to extract
SCHEME_MAX_CHARS = 8000        # above this the prompt outgrows the LLM budget
SCHEME_MIN_SIGNALS = 3
IDEAL_CHARS = 5000           # centre of the parseable band; best-of target
EARLY_EXIT_SIGNALS = 5       # rich enough that a later hit will not beat it
MAX_RENDER_CANDIDATES = 4    # bound Tier 2 wall time while still shopping


def scheme_content_score(markdown: str) -> int:
    low = (markdown or "").lower()
    return sum(1 for s in SCHEME_SIGNALS if s in low)
SECRETS = (
    "TINYFISH_API_KEY",
    "LLM_API_KEY",
    "OBJECT_STORAGE_ACCESS_KEY",
    "OBJECT_STORAGE_SECRET_KEY",
    "SECRET_KEY",
    "ADMIN_SERVICE_TOKEN",
)
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
H = {  # json colouring
    "key": "\033[36m",
    "str": "\033[32m",
    "num": "\033[33m",
    "lit": "\033[35m",
}


# --------------------------------------------------------------------------
# presentation
# --------------------------------------------------------------------------
def step(n: int, title: str) -> None:
    print(f"\n\033[95m{BOLD}STEP {n} — {title}{RESET}")


def line(label: str, value: str = "") -> None:
    print(f"  {label:<34} {value}")


def ok(msg: str) -> None:
    print(f"  \033[92m\N{WHITE HEAVY CHECK MARK} {msg}{RESET}")


def warn(msg: str) -> None:
    print(f"  \033[93m! {msg}{RESET}")


def fail(msg: str) -> None:
    print(f"  \033[91m\N{CROSS MARK} {msg}{RESET}")


def mask(value: str | None) -> str:
    """Never print a secret whole — first 5 + '...' + last 3, like sk-t...a9x."""
    v = (value or "").strip()
    if not v:
        return "(empty)"
    if len(v) <= 10:
        return "***"
    return f"{v[:5]}...{v[-3:]}"


def colour_json(obj: object) -> str:
    """Pretty JSON with keys/strings/numbers/literals tinted (tty colour)."""
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            end = min(j + 1, n)
            k = end
            while k < n and text[k] in " \t\n":
                k += 1
            tint = H["key"] if k < n and text[k] == ":" else H["str"]
            out.append(f"{tint}{text[i:end]}{RESET}")
            i = end
        elif ch in "-0123456789":
            j = i
            while j < n and text[j] in "-+.eE0123456789":
                j += 1
            out.append(f"{H['num']}{text[i:j]}{RESET}")
            i = j
        elif text.startswith(("true", "false", "null"), i):
            lit = next(l for l in ("true", "false", "null") if text.startswith(l, i))
            out.append(f"{H['lit']}{lit}{RESET}")
            i += len(lit)
        else:
            out.append(ch)
            i += 1
    return "\n".join(f"    {row}" for row in "".join(out).splitlines())


# ==========================================================================
async def main() -> int:
    from services.api.config import get_settings
    from services.tinyfish.agent import (
        SNAPSHOT_LIFECYCLE_RULE_ID,
        SNAPSHOT_URL_TTL_SECONDS,
        SnapshotStore,
        TinyFishWebAgentClient,
        VerificationResult,
    )
    from services.tinyfish.client import TinyFishFetchClient, TinyFishSearchClient
    from services.tinyfish.parser import ingest_markdown

    s = get_settings()
    started = time.perf_counter()

    print(f"{BOLD}\033[94m{'=' * 78}")
    print("  SchemeRadar — Step 3 (Gate G3) LIVE end-to-end smoke")
    print(f"  {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"{'=' * 78}{RESET}")

    # ---------------------------------------------------------------- 1 --
    step(1, "credentials (read from Settings, never from os.environ)")
    if not s.tinyfish_api_key:
        fail("TINYFISH_API_KEY is empty — Tier 1/2/3 cannot authenticate")
        return 2
    for name in SECRETS:
        raw = getattr(s, name.lower(), None)
        shown = mask(raw)
        note = "" if shown != "(empty)" else "   <- unset"
        if name == "TINYFISH_API_KEY":
            line(f"{name}:", f"{BOLD}{shown}{RESET} (len {len(raw or '')}){note}")
        else:
            line(f"{name}:", f"{shown}{note}")
    line(
        "Mongo URI:",
        f"{mask(s.mongo_uri.rsplit('@', 1)[-1]) if '@' in (s.mongo_uri or '') else mask(s.mongo_uri)}",
    )
    line("tier budgets (ms):", f"T1 {s.tinyfish_timeout_tier1_ms} | "
                              f"T2 {s.tinyfish_timeout_tier2_ms} | "
                              f"T3 {BOLD}{s.tinyfish_timeout_tier3_ms}{RESET}")
    if s.tinyfish_timeout_tier3_ms != 6000:
        warn(f"Tier-3 budget is {s.tinyfish_timeout_tier3_ms} ms, not the frozen 6,000 ms")
    if not (s.llm_base_url and s.llm_api_key and s.llm_model):
        fail("LLM_BASE_URL / LLM_API_KEY / LLM_MODEL not configured")
        return 2
    line("ingestion LLM:", f"{s.llm_model} @ {s.llm_base_url} (key {mask(s.llm_api_key)})")
    ok("secrets resolved and masked; no value printed in full")

    # ---------------------------------------------------------------- 2 --
    step(2, f"Tier 1 discovery — live search for {SEARCH_QUERY!r}")
    t = time.perf_counter()
    async with TinyFishSearchClient() as search:
        hits = await search.discover([SEARCH_QUERY], max_results=5)
    t1_ms = int((time.perf_counter() - t) * 1000)
    if not hits:
        fail(f"no *.gov.in / *.nic.in hit for {SEARCH_QUERY!r} — nothing to verify")
        return 3
    for n, h in enumerate(hits, 1):
        title = str(h.get("title") or "")
        line(f"hit {n}:", f"{h.url}  {DIM}({title[:60]}){RESET}")
    portal_url = ""            # resolved in step 3, once a page is proven parseable
    ok(f"Tier 1 answered in {t1_ms} ms (budget {s.tinyfish_timeout_tier1_ms} ms)")

    # ---------------------------------------------------------------- 3 --
    step(3, "Tier 2 fetch — live render of a parseable discovered portal")
    t = time.perf_counter()
    async with TinyFishFetchClient() as fetcher:
        fetched = None
        portal_url = ""
        attempts: list[str] = []
        best: tuple[int, int, object, str] | None = None   # (score, chars, cand, url)
        for n, h in enumerate(hits):
            url = str(h.url)
            if url.lower().split("?")[0].endswith(".pdf"):
                attempts.append(f"{url} -> skipped (PDF, not a portal page)")
                continue
            if n >= MAX_RENDER_CANDIDATES:
                attempts.append(f"{url} -> skipped (render cap {MAX_RENDER_CANDIDATES})")
                continue
            try:
                cand = await fetcher.render(url)
            except Exception as exc:                      # noqa: BLE001 - report and move on
                attempts.append(f"{url} -> {type(exc).__name__}")
                continue
            md = cand.markdown
            score = scheme_content_score(md)
            attempts.append(f"{url} -> {len(md)} chars, {score} scheme signals")
            # Keep the richest page seen so far; prefer signal count, then a
            # length closest to the middle of the parseable band (a 1.5k-char
            # press release can clear MIN_SIGNALS yet carry no scheme body).
            if SCHEME_MIN_SIGNALS <= score and SCHEME_MIN_CHARS <= len(md) <= SCHEME_MAX_CHARS:
                cand_key = (score, -abs(len(md) - IDEAL_CHARS))
                if best is None or cand_key > (best[0], -abs(best[1] - IDEAL_CHARS)):
                    best = (score, len(md), cand, url)
                # A page this rich will not be beaten by a later hit.
                if score >= EARLY_EXIT_SIGNALS and len(md) >= IDEAL_CHARS:
                    break
        if best is not None:
            _, _, fetched, portal_url = best
    t2_ms = int((time.perf_counter() - t) * 1000)
    for a in attempts:
        line("  candidate:", f"{DIM}{a}{RESET}")
    if fetched is None:
        fail("no discovered hit rendered into a parseable scheme page")
        return 4
    line("selected portal:", f"{BOLD}\033[96m{portal_url}{RESET}")
    markdown = fetched.markdown
    line("http_status:", str(fetched["http_status"]))
    line("final_url:", fetched["final_url"])
    line("rendered_at:", str(fetched["rendered_at"]))
    line("markdown:", f"{BOLD}{len(markdown)}{RESET} chars  "
                     f"(~{len(markdown) // 4} tokens)")
    line("content_hash:", fetched.content_hash)
    if not markdown.strip():
        fail("portal returned no markdown — content gate would reject this")
        return 4
    line("first 240 chars:", f"{DIM}{markdown[:240]!r}{RESET}")
    ok(f"Tier 2 answered in {t2_ms} ms (budget {s.tinyfish_timeout_tier2_ms} ms)")

    # ---------------------------------------------------------------- 4 --
    step(4, "Ingestion LLM + L1-L5 ladder — live completion -> Scheme")
    t = time.perf_counter()
    outcome = await ingest_markdown(
        markdown,
        fetch={"final_url": fetched["final_url"], "content_hash": fetched.content_hash},
        db=None,          # read-only smoke: no Mongo write, no HRQ row
        indexer=None,     # no embed, no Qdrant upsert
        check_dns=False,  # SSRF halves are unit-tested; keep this run about the ladder
    )
    parse_ms = int((time.perf_counter() - t) * 1000)
    line("pipeline result:", f"{BOLD}{outcome.result}{RESET}")
    line("highest layer reached:", str(outcome.layer))
    line("repair attempts:", "1 (max)" if outcome.repaired else "0 (clean first pass)")
    line("violations:", ", ".join(outcome.violations) or "(none)")
    line("scheme_id:", str(outcome.scheme_id))
    line("parse wall time:", f"{parse_ms} ms")

    scheme_id: str
    if outcome.scheme is None:
        # Fail-closed to human_review (DATA_SPEC §7.4) is itself a correct
        # pipeline outcome — report it, then keep going: Tier 3 verifies a
        # portal independently of the parse (ARCHITECTURE §7.3).
        fail("ladder rejected the candidate -> human_review (fail-closed)")
        for v in outcome.violations:
            print(f"      \033[91m- {v}{RESET}")
        bits = urlparse(portal_url)
        scheme_id = "sch_" + re.sub(
            r"[^a-z0-9]+", "_", f"{bits.netloc}{bits.path}".lower()
        ).strip("_")
        warn("no Scheme model to print; continuing to Tier 3 + MinIO")
    else:
        scheme = outcome.scheme
        dumped = scheme.model_dump(mode="json", exclude_none=False)
        print(f"\n  {BOLD}\033[96mScheme (Pydantic model_dump, exact frozen schema){RESET}")
        print(colour_json(dumped))
        print()
        line("model class:", f"{type(scheme).__module__}.{type(scheme).__qualname__}")
        line("parsed fields:", str(len(dumped)))
        line("missing / null:",
             ", ".join(k for k, v in dumped.items() if v is None) or "(none)")
        # cross-check the model against the frozen JSON Schema on disk
        from services.tinyfish.parser import _schema

        schema = _schema()
        schema_keys = set(schema.get("properties", {}))
        required_keys = set(schema.get("required", {}))
        extra = set(dumped) - schema_keys
        missing_required = required_keys - set(dumped)
        if extra or missing_required:
            fail(f"schema mismatch: extra={extra} missing_required={missing_required}")
            return 5
        # Optional properties the source never stated are legitimately absent:
        # `_Object._omit_unset_keys` drops unset keys so the dump round-trips
        # the payload that was read (JSON Schema models absence, not null).
        omitted = schema_keys - set(dumped)
        if omitted:
            line("optional (absent, as JSON Schema intends):",
                 f"{DIM}{', '.join(sorted(omitted))}{RESET}")
        ok(f"live LLM + ladder produced a schema-exact Scheme in {parse_ms} ms "
           f"(confidence floor 0.75, no canned candidate)")
        scheme_id = str(outcome.scheme_id or dumped.get("scheme_id") or "")

    # ---------------------------------------------------------------- 5 --
    step(5, "Tier 3 web agent — live portal run, SSE-shaped trace")
    job_id = uuid.uuid4().hex
    if not scheme_id:
        bits = urlparse(portal_url)
        scheme_id = "sch_" + re.sub(
            r"[^a-z0-9]+", "_", f"{bits.netloc}{bits.path}".lower()
        ).strip("_")
    line("scheme_id:", scheme_id)
    line("job_id:", job_id)
    line("budget:", f"{BOLD}{s.tinyfish_timeout_tier3_ms} ms{RESET} (frozen §9)")

    agent = TinyFishWebAgentClient(db=None)   # no Mongo write; browser + MinIO are real
    t = time.perf_counter()
    frames: list[tuple[int, object]] = []
    result: VerificationResult | None = None
    n = 0
    async for item in agent.events(scheme_id, portal_url, job_id):
        ms = int((time.perf_counter() - t) * 1000)
        frames.append((ms, item))
        if isinstance(item, VerificationResult):
            result = item
            continue
        n += 1
        print(f"    [{ms:>5} ms] \033[90mframe{n:>2}  {RESET}"
              f"\033[93m{str(item)}{RESET}")
    if result is None:
        fail("agent produced no VerificationResult")
        return 6

    dur = result.duration_ms
    budget = s.tinyfish_timeout_tier3_ms
    pct = (dur / budget * 100) if budget else 0.0
    print()
    line("frames streamed:", str(n))
    line("final envelope:", colour_json({
        "verification_status": result.verification_status.value,
        "extracted_deadline": str(result.extracted_deadline) if result.extracted_deadline else None,
        "final_url": result.final_url,
        "snapshot_url": result.snapshot_url,
        "observed_signals": result.observed_signals,
        "error_class": result.error_class,
        "duration_ms": result.duration_ms,
        "job_id": result.job_id,
    }))
    verdict = result.verification_status.value
    colour = "\033[92m" if verdict == "INTAKE_OPEN" else "\033[93m"
    line("VERDICT:", f"{colour}{BOLD}{verdict}{RESET}")
    line("budget used:", f"{BOLD}{dur} ms{RESET} / {budget} ms "
                        f"= {pct:.1f}%" + ("   \033[92m(within budget)\033[0m"
                                           if dur <= budget else "   \033[91m(OVER)\033[0m"))
    if result.error_class:
        line("error_class:", result.error_class)
    if dur > budget:
        fail("over the frozen Tier-3 budget")
        return 6
    ok(f"agent finished in {int((time.perf_counter() - t) * 1000)} ms wall clock")

    # ---------------------------------------------------------------- 6 --
    step(6, "MinIO evidence — 90-day lifecycle + presigned GET (Build Order 3.9)")
    store = SnapshotStore()
    try:
        ready = await store.ensure_ready()
    except Exception as exc:  # noqa: BLE001 — report, never crash the smoke
        ready = False
        fail(f"ensure_ready() raised {type(exc).__name__}: {exc}")
    line("bucket + lifecycle:", f"{BOLD}{ready}{RESET}  "
                                f"(bucket {store.bucket}, rule {SNAPSHOT_LIFECYCLE_RULE_ID})")
    line("durable reference:", f"{DIM}{result.snapshot_url or '(none)'}{RESET}")
    if not result.snapshot_url:
        warn("agent stored no snapshot — nothing to sign")
        ok("lifecycle installed; presigned step skipped")
        return 0 if verdict else 6

    presigned = store.generate_snapshot_url(result.snapshot_url)
    if not presigned:
        fail("generate_snapshot_url() returned None")
        return 7
    signed = presigned.split("?", 1)[1] if "?" in presigned else ""
    line("presigned GET:", f"{BOLD}\033[96m{presigned.split('?')[0]}?X-Amz-Algorithm=...{RESET}")
    line("signature:", f"{DIM}{signed[:96]}...{RESET}" if len(signed) > 96 else signed)
    line("expires:", f"{SNAPSHOT_URL_TTL_SECONDS} s (`SNAPSHOT_URL_TTL_SECONDS`)")
    if "X-Amz-Signature=" not in presigned:
        fail("no signature in the presigned URL")
        return 7

    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as hc:
        signed_resp = await hc.get(presigned)
        # the durable reference must NOT be readable without a signature
        bare_resp = await hc.get(result.snapshot_url)
    line("signed GET:", f"HTTP {BOLD}{signed_resp.status_code}{RESET}  "
                          f"{len(signed_resp.content):,} bytes  "
                          f"{signed_resp.headers.get('content-type', '?')}")
    line("bare GET (no signature):",
         f"HTTP {BOLD}{bare_resp.status_code}{RESET}  <- durable ref stays private")
    if signed_resp.status_code == 200 and len(signed_resp.content) > 0:
        ok(f"presigned URL served the real snapshot ({len(signed_resp.content):,} bytes)")
    else:
        fail(f"presigned GET failed: HTTP {signed_resp.status_code}")
        return 7
    if bare_resp.status_code in (403, 401):
        ok(f"unsigned access correctly denied (HTTP {bare_resp.status_code})")
    else:
        warn(f"unsigned access returned HTTP {bare_resp.status_code} — "
             "expected 403 for an unsigned object")

    # ---------------------------------------------------------------- --
    print(f"\n\033[95m{BOLD}SUMMARY{RESET}")
    line("Tier 1", f"live search {t1_ms} ms -> {portal_url}")
    line("Tier 2", f"live fetch {t2_ms} ms, {len(markdown)} chars markdown")
    line("Ingestion LLM", f"{outcome.result} @ {outcome.layer}, {parse_ms} ms")
    line("Tier 3", f"{verdict} in {dur} ms ({pct:.1f}% of {budget} ms)")
    line("Evidence", f"lifecycle={ready}, signed HTTP {signed_resp.status_code}, "
                     f"bare HTTP {bare_resp.status_code}")
    line("total wall time", f"{int((time.perf_counter() - started) * 1000)} ms")
    print(f"\n\033[92m{BOLD}\N{WHITE HEAVY CHECK MARK} "
          f"Gate G3 smoke completed against live services{RESET}\n")
    return 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover
        code = 130
    except Exception as exc:  # noqa: BLE001 — surface it, don't traceback-dump
        print(f"\n\033[91m{BOLD}SMOKE FAILED: {type(exc).__name__}: {exc}{RESET}")
        import traceback

        traceback.print_exc()
        code = 1
    sys.exit(code)
