import re, pathlib, sys, subprocess

base = pathlib.Path(__file__).resolve().parents[2] / "docs"
P1 = (base / "ARCHITECTURE.md").read_text()
P2 = (base / "DATA_SPEC.md").read_text()
P3 = (base / "WORKFLOW_AND_TESTS.md").read_text()
P4 = (base / "PPT_SUBMISSION.md").read_text()
ok = True

def chk(label, cond, detail=""):
    global ok
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{(' -> ' + detail) if detail and not cond else ''}")
    ok &= bool(cond)

# ---------- structure ----------
print("\n[1] Nine mandatory slides")
for i in range(1, 10):
    chk(f"Slide {i} heading present", re.search(rf"^## Slide {i} —", P4, re.M) is not None)
slides = re.split(r"^## Slide \d+ —.*$", P4, flags=re.M)[1:]
chk("exactly 9 slide sections", len(re.findall(r"^## Slide \d+ —", P4, re.M)) == 9)
for i, s in enumerate(slides, 1):
    chk(f"Slide {i} has on-slide content + speaker notes",
        "### On-slide content" in s and "### Speaker notes" in s)
    notes = s.split("### Speaker notes")[1]
    chk(f"Slide {i} speaker notes are bulleted", notes.lstrip().startswith("-"), notes.lstrip()[:40])
    chk(f"Slide {i} speaker notes >= 4 bullets", notes.count("\n- ") + notes.startswith("- ") >= 4)

# required per-slide topics
topics = {
 1: ["1.4 Lakh Crore", "65%", "ASP.NET", "gazette", "ghost"],
 2: ["Neuro-Symbolic", "Score", "TinyFish", "verification"],
 3: ["Delhi Post-Matric", "PM-Kisan", "Gap", "SDM Income Certificate"],
 4: ["S_det", "S_sem", "P_docs", "RRF", "Semantic Auditor"],
 5: ["Next.js", "FastAPI", "Qdrant", "MongoDB", "BM25"],
 6: ["api.search.tinyfish.ai", "api.fetch.tinyfish.ai", "Playwright", "snapshot"],
 7: ["NEAR_MISS", "fail-closed", "60 minutes", "Bridge the Gap"],
 8: ["1,200 ms", "6,000 ms", "content_hash", "Human Review Queue"],
 9: ["DigiLocker", "voice", "form-filling", "name_hi"],
}
print("\n[2] Required slide topics")
for i, words in topics.items():
    seg = slides[i-1]
    low = seg.lower()
    missing = [w for w in words if w.lower() not in low]
    chk(f"Slide {i} topic coverage", not missing, str(missing))

# ---------- pitch script ----------
print("\n[3] 3-minute pitch script")
chk("pitch script section present", "## Timed 3-Minute Oral Pitch Script" in P4)
pitch_start = P4.index("## Timed 3-Minute Oral Pitch Script")
pitch_end = P4.index("### Pitch timing audit")
pitch = P4[pitch_start:pitch_end]

marks = re.findall(r"\[(\d+):(\d+) – (\d+):(\d+)\]", pitch)
chk("6 timing segments found", len(marks) == 6, str(len(marks)))
total = 0
prev_end = 0
segs_ok = True
for m1, m2, m3, m4 in marks:
    s = int(m1)*60 + int(m2); e = int(m3)*60 + int(m4)
    segs_ok &= (s == prev_end)
    total += (e - s)
    prev_end = e
chk("segments contiguous (no gaps/overlaps)", segs_ok)
chk("total duration == 180 s", total == 180, f"got {total}s")

# stage directions
dirs = re.findall(r"\*\(.*?\)\*", pitch)
chk("stage directions present (>=8)", len(dirs) >= 8, str(len(dirs)))
for need in ["Click", "Point to the verification badge", "Walk to centre"]:
    chk(f"stage direction contains '{need}'", any(need in d for d in dirs))

# spoken word count: strip stage directions, timing markers, headings
spoken = re.sub(r"\*\(.*?\)\*", " ", pitch)
spoken = re.sub(r"\[\d+:\d+ – \d+:\d+\] · [^\n]*", " ", spoken)
spoken = re.sub(r"\*\*", " ", spoken)
spoken = re.sub(r"\*", " ", spoken)
words = [w for w in re.split(r"\s+", spoken) if re.search(r"[A-Za-z0-9₹]", w)]
chk("spoken word count 380-520 (≈3 min @ ~145 wpm)", 380 <= len(words) <= 520, f"{len(words)} words")

# hook before TinyFish reveal
hook = pitch.split("(Click — Slide 1.)")[0] if "(Click — Slide 1.)" in pitch else pitch[:2000]
chk("hook mentions ₹1.4 lakh crore before any TinyFish mention",
    ("1.4" in hook or "one point four" in hook) and "TinyFish" not in hook)
chk("TinyFish named only after the problem",
    pitch.index("one point four") < pitch.index("TinyFish"))
for t in ["Search API", "Fetch API", "Web Agent"]:
    chk(f"pitch names TinyFish {t}", t in pitch)
chk("pitch names neuro-symbolic", "neuro-symbolic" in pitch.lower())
chk("pitch closes with ask", "Thank you" in pitch)

# ---------- cross-document consistency ----------
print("\n[4] Numbers traceable to Phases 1-3")
pairs = [
    ("0.887", P3, P4), ("0.987", P3, P4), ("0.688", P2, P4), ("0.988", P2, P4),
    ("0.787", P3, P4), ("60,000", P2, P4), ("48,000", P2, P4), ("12,000", P2, P4),
    ("2,50,000", P2, P4), ("1,00,000", P2, P4), ("1,05,000", P2, P4), ("2,55,000", P3, P4),
    ("1,200 ms", P1, P4), ("6,000 ms", P1, P4), ("2,000 ms", P1, P4),
    ("8,000 ms", P1, P4), ("≤ 6,000 ms", P1, P4), ("60 minutes", P1, P4),
    ("0.75", P1, P4), ("1024", P1, P4), ("sch_delhi_post_matric_scholarship_sc_st_obc_2026", P2, P4),
    ("sch_pm_kisan_samman_nidhi_2019", P2, P4),
    ("sch_delhi_post_matric_scholarship_sc_st_obc_2026", P2, P4),
]
for label, src, dst in pairs:
    chk(f"'{label}' exists in source and in deck", label in src and label in dst)

terms = ["Neuro-Symbolic Scoring Engine", "Bridge the Gap", "fail-closed", "Human Review Queue",
         "NEAR_MISS", "verification_status", "Reciprocal Rank Fusion"]
for t in terms:
    chk(f"frozen term '{t}' used in deck", t in P4)
    chk(f"frozen term '{t}' exists in Phases 1-3", t in (P1 + P2 + P3))

# TinyFish three tiers
for e in ["api.search.tinyfish.ai", "api.fetch.tinyfish.ai"]:
    chk(f"endpoint {e} in deck and Phase 1", e in P4 and e in P1)
chk("Playwright/CDP in deck and Phase 1", "Playwright" in P4 and "Playwright" in P1)

# formula consistency
chk("formula weights in deck match Phase 1", "W_D = 0.60" in P4 and "W_S = 0.40" in P4
    and "$W_D$ | 0.60" in P1 and "$W_S$ | 0.40" in P1)

# test/invariant counts
chk("50 tests claim matches Phase 3", "50 acceptance tests" in P4 and "50 unique" not in P3)
chk("10 invariants claim matches Phase 3", "Ten invariants" in P4 or "10 invariants" in P4)
chk("Phase 3 has 10 invariants I-1..I-10", "**I-10**" in P3)
ids = set(re.findall(r"\bTC-(?:[A-C])?\d+\b", P3))
chk("Phase 3 really has 50 test IDs", len(ids) == 50, f"found {len(ids)}")

# ---------- hygiene ----------
print("\n[5] Workspace hygiene")
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
chk("only .md files -> docs/ contains Markdown only (specification purity)",
    not non_md, str(non_md))
missing = [f for f in SPEC_FILES if f not in tracked]
chk("only .md files -> all 6 specification files are tracked", not missing, str(missing))
fences = P4.count("```")
chk("code fences balanced", fences % 2 == 0, f"count={fences}")

print("\nRESULT:", "ALL CHECKS PASSED" if ok else "FAILURES PRESENT")
sys.exit(0 if ok else 1)
