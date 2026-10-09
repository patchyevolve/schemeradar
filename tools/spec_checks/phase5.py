import re, pathlib, sys, subprocess

root = pathlib.Path(__file__).resolve().parents[2]
docs = root / "docs"
P1 = (docs / "ARCHITECTURE.md").read_text()
P2 = (docs / "DATA_SPEC.md").read_text()
P3 = (docs / "WORKFLOW_AND_TESTS.md").read_text()
P4 = (docs / "PPT_SUBMISSION.md").read_text()
R  = (root / "README.md").read_text()
B  = (docs / "BUILD_ORDER.md").read_text()
ok = True

def chk(label, cond, detail=""):
    global ok
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{(' -> ' + detail) if detail and not cond else ''}")
    ok &= bool(cond)

print("\n[1] README.md structure")
for s in ["Problem & Solution", "System Architecture", "Neuro-Symbolic Engine",
          "Hybrid Search", "TinyFish 3-Tier Integration", "Quickstart / Local Setup"]:
    chk(f"section '{s}' present", s in R)
toc = R[R.find("## Table of Contents"):R.find("## 📌 Repository Status")]
for s in ["Solution", "Architecture", "TinyFish", "Quickstart"]:
    chk(f"TOC mentions '{s}'", s in toc)
# banner/title
chk("banner title present", "# 📡 SchemeRadar" in R)
chk("tagline present", "Neuro-Symbolic Engine for Civic Entitlement" in R)
chk("badges present", R.count("img.shields.io") >= 4)
# tinyfish 3 tiers explicitly
for t in ["Tier 1", "Tier 2", "Tier 3",
          "api.search.tinyfish.ai", "api.fetch.tinyfish.ai",
          "TinyFishSearchClient", "TinyFishFetchClient", "TinyFishWebAgentClient"]:
    chk(f"README names {t}", t in R)
# quickstart: docker + venv
chk("docker compose instruction", "docker compose up -d" in R)
chk("python venv instruction", "python3.14 -m venv .venv" in R)
chk("venv activate instruction", "source .venv/bin/activate" in R)
chk("node/pnpm instruction", "pnpm install" in R)
# mermaid blocks
def fence_count(t): return t.count("```")
chk("README fences balanced", fence_count(R) % 2 == 0, str(fence_count(R)))
chk("README has >=2 mermaid diagrams", R.count("```mermaid") >= 2, str(R.count("```mermaid")))
# no application source code
bad_langs = re.findall(r"```(python|typescript|javascript|tsx|jsx|go|rust|java|c\+\+)\b", R + B)
chk("no app-language code fences", not bad_langs, str(bad_langs))

print("\n[2] BUILD_ORDER.md structure")
# the 5 mandated steps in order
steps = ["Step 1 — Repository & Environment Setup",
         "Step 2 — Seed Qdrant & MongoDB",
         "Step 3 — TinyFish Crawler Services",
         "Step 4 — Neuro-Symbolic Engine & FastAPI Routes",
         "Step 5 — Next.js Dashboard"]
pos = []
for s in steps:
    i = B.find(s)
    pos.append(i)
    chk(f"'{s}' present", i != -1)
chk("steps appear in mandated order", all(pos[i] < pos[i+1] for i in range(len(pos)-1)), str(pos))
chk("test step present", "50 Tests" in B and "Step 6" in B)
chk("freeze step present", "Step 7 — Freeze" in B)
chk("deadline Oct 18 stated", "October 18, 2026" in B)
chk("hackathon dates stated", "October 21–22, 2026" in B)
chk("BUILD_ORDER fences balanced", fence_count(B) % 2 == 0, str(fence_count(B)))
chk("BUILD_ORDER has mermaid dependency graph", "```mermaid" in B)

print("\n[3] Cross-document number consistency (README/BUILD vs Phases 1-4)")
num_pairs = [
    ("0.887", P3, R), ("0.987", P3, R), ("0.787", P3, R), ("0.688", P2, R),
    ("0.988", P2, R), ("0.887", P3, B), ("0.688", P2, B),
    ("0.988", P2, B), ("1,200 ms", P1, R), ("6,000 ms", P1, R),
    ("1,200 ms", P1, B), ("6,000 ms", P1, B), ("2,000 ms", P1, R),
    ("8,000 ms", P1, R), ("4,000 ms", P1, R), ("60 minutes", P1, R),
    ("1.4 Lakh Crore", P1, R), ("65%", P4, R), ("65%", P4, B),
    ("+0.30", P4, R), ("+0.30", P4, B), ("0.737", P3, B), ("0.60", P1, R),
    ("0.40", P1, R), ("k = 60", P1, R), ("1024", P1, R),
    ("k1 = 1.2", P1, R), ("b = 0.75", P1, R), ("0.75", P1, R),
    ("2,50,000", P2, R), ("5,000", P3, R),
    ("20,000 ms", P1, R), ("top-30", P1, B),
]
for label, src, dst in num_pairs:
    chk(f"'{label}' in source & target", label in src and label in dst)

print("\n[4] Frozen terms used verbatim in Phase 5")
terms = ["ProfileContext", "gate_trace", "score_breakdown", "verification_status",
         "NEAR_MISS", "Human Review Queue", "Bridge the Gap Checklist",
         "Neuro-Symbolic Scoring Engine", "S_det", "S_sem", "P_docs",
         "scheme_id", "documents_in_hand", "missing_documents",
         "sch_delhi_post_matric_scholarship_sc_st_obc_2026", "sch_pm_kisan_samman_nidhi_2019",
         "INTAKE_OPEN", "UNREACHABLE", "BLOCKED", "UNVERIFIED", "PENDING",
         "BAAI/bge-m3", "content_hash", "ingestion_audit", "verification_logs"]
for t in terms:
    in_src = t in (P1 + P2 + P3 + P4)
    in_new = (t in R) or (t in B)
    chk(f"'{t}' frozen in Phases 1-4 AND used in Phase 5", in_src and in_new)

print("\n[5] Endpoints & module paths")
for e in ["/api/v1/profile/qualify", "/api/v1/schemes/{scheme_id}",
          "/api/v1/verify/{scheme_id}", "/api/v1/checklist/{scheme_id}",
          "/api/v1/verify/stream/{job_id}", "/api/v1/admin/ingest/refresh"]:
    chk(f"endpoint {e}", e in P1 and (e in R or e in B))
for d in ["web/", "services/api/", "services/scoring/", "services/tinyfish/"]:
    chk(f"module path {d}", d in P1 and d in B)

print("\n[6] Test & invariant claims")
chk("50 tests claimed in README", "50 acceptance tests" in R)
chk("10 invariants claimed in README", "10 invariants" in R)
chk("50 tests claimed in BUILD_ORDER", "50" in B and "acceptance test" in B)
ids = set(re.findall(r"\bTC-(?:[A-C])?\d+\b", P3))
chk("Phase 3 really has 50 test IDs", len(ids) == 50, f"{len(ids)}")
chk("I-1..I-10 present in Phase 3", "**I-10**" in P3)
chk("README lists all 10 invariants", all(f"**I-{i}**" in R for i in range(1, 11)))

print("\n[7] Hygiene")
WS = pathlib.Path(__file__).resolve().parents[2]
tracked = subprocess.run(["git", "-C", str(WS), "ls-files"],
                         capture_output=True, text=True).stdout.split()
# Invariant that survived the no-source-code constraint being lifted:
# the SPECIFICATION SUITE itself must remain Markdown-only, and all six
# specification files must actually be published.
SPEC_FILES = ["README.md", "docs/ARCHITECTURE.md", "docs/DATA_SPEC.md",
              "docs/WORKFLOW_AND_TESTS.md", "docs/PPT_SUBMISSION.md",
              "docs/BUILD_ORDER.md"]
docs_files = sorted(str(p.relative_to(WS)) for p in (WS / "docs").rglob("*") if p.is_file())
non_md = [f for f in docs_files if not f.endswith(".md")]
chk("workspace docs/ contains Markdown only (specification purity)",
    not non_md, str(non_md))
missing = [f for f in SPEC_FILES if f not in tracked]
chk("workspace all 6 specification files are tracked", not missing, str(missing))
chk("README.md exists at root", (root / "README.md").exists())
chk("docs/BUILD_ORDER.md exists", (docs / "BUILD_ORDER.md").exists())
# validators still green
import subprocess
checks_dir = root / "tools" / "spec_checks"
for v in ["phase2.py", "phase3.py", "phase4.py"]:
    r = subprocess.run([sys.executable, str(checks_dir / v)], capture_output=True, text=True)
    chk(f"spec_checks/{v} still green", r.returncode == 0, r.stdout[-200:] + r.stderr[-200:])

print("\n[8] TinyFish boundary lint (BUILD_ORDER §5.4 box 1)")
# ARCHITECTURE 5.5 boundary rule: "No other module may call TinyFish
# endpoints directly" -- the gateway (services/tinyfish/) owns the hosts,
# retries, rate limits and audit logging in one place.  This is the lint rule
# that keeps that true: outside the gateway, only Settings may even *know* an
# endpoint host, and nothing outside may hold a host and an HTTP client at once.
GATEWAY = (root / "services" / "tinyfish").resolve()
HOSTS = ("api.search.tinyfish.ai", "api.fetch.tinyfish.ai")
HTTP_CLIENTS = ("httpx", "requests", "urllib.request", "aiohttp")
# services/api/config.py DECLARES the endpoint (Settings/.env must be able to
# override it) but must never call it.
DECLARES_ONLY = {"services/api/config.py"}
knows, calls = [], []
for base in ("services", "web", "tools"):
    top = root / base
    if not top.exists():
        continue
    for path in sorted(top.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root)
        if GATEWAY in path.parents or path.parent == GATEWAY:
            continue                      # the gateway itself
        if str(rel).startswith("tools/spec_checks"):
            continue                      # validators quote the specs verbatim
        text = path.read_text(encoding="utf8", errors="replace")
        if not any(h in text for h in HOSTS):
            continue
        if str(rel) not in DECLARES_ONLY:
            knows.append(str(rel))
        if any(m in text for m in HTTP_CLIENTS):
            calls.append(str(rel))
chk("no module outside services/tinyfish/ knows a TinyFish endpoint",
    not knows, str(knows))
chk("declaring a host in Settings never comes with an HTTP client",
    not calls, str(calls))

print("\nRESULT:", "ALL CHECKS PASSED" if ok else "FAILURES PRESENT")
sys.exit(0 if ok else 1)
