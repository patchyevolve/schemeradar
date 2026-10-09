import json, re, pathlib, sys, math, subprocess

base = pathlib.Path(__file__).resolve().parents[2] / "docs"
P1 = (base / "ARCHITECTURE.md").read_text()
P2 = (base / "DATA_SPEC.md").read_text()
P3 = (base / "WORKFLOW_AND_TESTS.md").read_text()
ok = True

def chk(label, cond, detail=""):
    global ok
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{(' -> ' + detail) if detail and not cond else ''}")
    ok &= bool(cond)

# ---------- 1. structural integrity ----------
print("\n[1] Document structure")
for name, txt in [("ARCHITECTURE", P1), ("DATA_SPEC", P2), ("WORKFLOW_AND_TESTS", P3)]:
    fences = txt.count("```")
    chk(f"{name}: code fences balanced (even)", fences % 2 == 0, f"count={fences}")
    subs = len(re.findall(r"^\s*subgraph\s", txt, re.M))
    ends = len(re.findall(r"^\s*end\s*$", txt, re.M))
    chk(f"{name}: mermaid subgraph/end balanced", subs == ends, f"subgraph={subs} end={ends}")

mermaid_blocks = re.findall(r"```mermaid\n(.*?)```", P3, re.S)
chk("WORKFLOW: has >=1 mermaid diagram", len(mermaid_blocks) >= 1, str(len(mermaid_blocks)))

# ---------- 2. JSON blocks parse ----------
print("\n[2] JSON instances in WORKFLOW_AND_TESTS")
blocks = re.findall(r"```json\n(.*?)```", P3, re.S)
chk("json block count >= 1", len(blocks) >= 1, str(len(blocks)))
for i, b in enumerate(blocks):
    try:
        obj = json.loads(b)
        chk(f"json block {i} parses", True)
        if isinstance(obj, dict) and "readiness" in obj:
            r = obj["readiness"]
            chk("GapChecklist readiness arithmetic (total-held == blocking_missing)",
                r["documents_total"] - r["documents_held"] == r["blocking_missing"],
                str(r))
            chk("GapChecklist already_covered length == documents_held",
                len(obj["already_covered"]) == r["documents_held"],
                f"{len(obj['already_covered'])} vs {r['documents_held']}")
            chk("GapChecklist score/band consistent (0.887 >= 0.80 => HIGH)",
                obj["band"] == "HIGH" and obj["score"] >= 0.80, f"{obj['score']} {obj['band']}")
            st = obj["steps"][0]
            chk("score_if_completed = score + charged_penalty",
                abs(st["score_if_completed"] - (obj["score"] + st["charged_penalty"])) < 1e-9,
                f"{st['score_if_completed']} vs {obj['score']}+{st['charged_penalty']}")
            chk("readiness.P_docs == sum(charged_penalty of steps)",
                abs(r["P_docs"] - sum(s["charged_penalty"] for s in obj["steps"])) < 1e-9)
            chk("Step document_id present in already_covered? no (missing expected)",
                st["document_id"] not in obj["already_covered"])
    except Exception as e:
        chk(f"json block {i} parses", False, str(e))

# ---------- 3. arithmetic of every quoted score ----------
print("\n[3] Score arithmetic")
W_D, W_S, P_CAP = 0.60, 0.40, 0.60
S_SEM = 0.968
def score(sdet, sem, p):
    return round(max(0.0, min(1.0, W_D*sdet + W_S*sem - p)), 3)
cases = [
    ("Edge Case A", 1, S_SEM, 0.10, 0.887),
    ("A: holds SDM cert", 1, S_SEM, 0.00, 0.987),
    ("A: holds neither", 1, S_SEM, 0.20, 0.787),
    ("A4 = Phase2 Scenario B", 1, S_SEM, 0.15, 0.837),
    ("Phase2 Scenario A", 1, S_SEM, 0.25, 0.737),
    ("worst case S_sem=0, P_cap", 1, 0.0, 0.60, 0.000),
]
for label, sdet, sem, p, expect in cases:
    got = score(sdet, sem, p)
    chk(f"{label}: {got} == {expect}", abs(got - expect) < 5e-4, f"got {got} want {expect}")

# band boundaries
def band(s):
    if s >= 0.80: return "HIGH"
    if s >= 0.60: return "MEDIUM"
    if s >= 0.30: return "LOW"
    return "SUPPRESSED"
chk("band 0.887 -> HIGH", band(0.887) == "HIGH")
chk("band 0.787 -> MEDIUM", band(0.787) == "MEDIUM")
chk("band 0.737 -> MEDIUM", band(0.737) == "MEDIUM")
chk("band 0.837 -> HIGH", band(0.837) == "HIGH")
chk("band 0.688 -> MEDIUM", band(0.688) == "MEDIUM")
chk("band 0.7999 -> MEDIUM / 0.8000 -> HIGH", band(0.7999) == "MEDIUM" and band(0.8000) == "HIGH")

# near-miss boundary ladder
ceil = 250000
def route(inc, ceil, delta=0.05):
    if inc <= ceil: return "PASS"
    d = (inc - ceil) / max(ceil, 1)
    return "NEAR_MISS" if d <= delta + 1e-12 else "DISCARD"
for inc, want in [(240000,"PASS"),(250000,"PASS"),(255000,"NEAR_MISS"),
                  (262500,"NEAR_MISS"),(262501,"DISCARD")]:
    got = route(inc, ceil)
    chk(f"income {inc} -> {want}", got == want, f"got {got}")
d = (255000-250000)/250000
chk("Delta(255000,250000) == 0.02", abs(d-0.02) < 1e-12, str(d))
d2 = (105000-100000)/100000
chk("Delta OBC 105000/100000 == 0.05 (near-miss, <=)", abs(d2-0.05) < 1e-12 and d2 <= 0.05)
chk("delta_income 0.05 present in P1 and P3", "0.05" in P1 and "\\delta_{income} = 0.05" in P3)
chk("delta_age 1.0 years in P3", "\\delta_{age} = 1.0" in P3 or "\\Delta_{age} = 1.0" in P3)

# ---------- 4. cross-document consistency ----------
print("\n[4] Cross-document consistency")
shared_consts = [
    ("W_D 0.60", "$W_D$ | 0.60" in P1, "W_D = 0.60" in P3 or "$W_D = 0.60$" in P3),
    ("W_S 0.40", "$W_S$ | 0.40" in P1, "W_S = 0.40" in P3 or "$W_S = 0.40$" in P3),
    ("P_cap 0.60", "0.60" in P1 and "P_{cap}" in P1, "P_{cap} = 0.60" in P3),
    ("RRF k=60", "60" in P1, "RRF $k = 60$" in P3 or "k = 60" in P3),
    ("top-N = 5", "top-N = 5" in P1, "top-**N = 5**" in P3 or "top-N = 5" in P3),
    ("concurrency 3", "concurrency cap of 3" in P1, "3 browsers" in P3),
    ("tier3 timeout 6000",
     "Tier-3 per-portal timeout" in P1 and "6,000 ms" in P1,
     "Per-portal hard timeout | 6,000 ms" in P3),
    ("freshness 60 min", "60 minutes" in P1 and "60 minutes" in P2, "60 minutes" in P3),
    ("snapshot key", "snapshots/{scheme_id}/{job_id}.png" in P1, "snapshots/{scheme_id}/{job_id}.png" in P3),
    ("bge-m3/1024 only in P1&P2", "bge-m3" in P1, "bge-m3" not in P3),
]
for label, a, b in shared_consts:
    chk(f"{label}: P1 ok, P3 ok", a and b, f"P1={a} P3={b}")

enums = ["INTAKE_OPEN","INTAKE_CLOSED","UNREACHABLE","BLOCKED","UNVERIFIED","PENDING"]
chk("all 6 verification_status values in P3", all(e in P3 for e in enums))
chk("all 6 present in P1 and P2", all(e in P1 for e in enums) and all(e in P2 for e in enums))
bands = ["HIGH","MEDIUM","LOW","SUPPRESSED","NEAR_MISS"]
chk("all 5 bands in P1 and P3", all(b in P1 for b in bands) and all(b in P3 for b in bands))
chk("band thresholds identical", "0.80" in P1 and "0.80" in P3 and "0.60" in P1 and "0.60" in P3
    and "0.30" in P1 and "0.30" in P3)

# endpoints named in P1 must be used in P3
eps = ["/api/v1/profile/qualify", "/api/v1/checklist/{scheme_id}",
       "/api/v1/verify/stream/{job_id}", "/api/v1/schemes/{scheme_id}"]
chk("P1 endpoints referenced in P3", all(e in P1 for e in eps) and all(e in P3 for e in eps))

# field names must exist in P2 schema
fields = ["verification_status","verified_at","extracted_deadline","snapshot_url",
          "verification_job_id","last_known_status","missing_documents","score_breakdown",
          "gate_trace","charged_penalty","is_stale","documents_in_hand"]
missing = [f for f in fields if f not in P2]
chk("all Phase-3-referenced fields exist in Phase 2", not missing, str(missing))

# error_class: Phase 2 five + ssrf_blocked disclosed in P3
p2_ec = ["timeout","captcha","waf_block","http_5xx","dns_failure"]
chk("Phase 2 error_class set covered in P3", all(e in P3 for e in p2_ec))
chk("ssrf_blocked disclosed as additive delta in P3", "ssrf_blocked" in P3 and "additive" in P3)
chk("MAX_AGENT_RETRIES declared in delta", "MAX_AGENT_RETRIES" in P3 and "Constants Delta" in P3)

# ---------- 5. required criteria coverage ----------
print("\n[5] Phase 3 deliverable coverage")
criteria = [
    ("1a Profile Input -> Processing -> Dashboard", all(t in P3 for t in ["Stage 1", "Stage 2", "Stage 3", "Profile Wizard"])),
    ("1b Score band display rules HIGH/MEDIUM/LOW/NEAR_MISS", "Score band presentation rules" in P3),
    ("1c Bridge the Gap checklist explained", "Bridge the Gap" in P3 and "GapChecklist" in P3),
    ("2 navigate", "Navigate with wait_until" in P3),
    ("2 bypass popups", "notice_popup_dismissed" in P3 and "POPUP_DISMISS" in P3),
    ("2 detect submit control enabled/disabled", "submit_control_enabled" in P3 and "submit_control_disabled" in P3),
    ("2 parse date strings", "Date parsing & normalisation" in P3),
    ("2 viewport snapshot", "Viewport screenshot" in P3),
    ("2 return VerificationStatus", "Verdict decision table" in P3),
    ("3A high-friction doc + alternative", "Edge Case A" in P3 and "provisional" in P3),
    ("3B borderline income 255000/250000", "2,55,000" in P3 and "2,50,000" in P3),
    ("3C portal down / captcha", "Edge Case C" in P3 and "http_5xx" in P3),
    ("links back to Phase 1 formulas", "Phase 1 §6.1" in P3 and "Phase 1 §6.2" in P3),
    ("links back to Phase 2 schemas", "Phase 2 §2.5.1" in P3 and "Phase 2 §6" in P3),
    ("practical tests present", P3.count("TC-") >= 30),
]
for label, c in criteria:
    chk(label, c)

tc_ids = sorted(set(re.findall(r"\bTC-[A-C]?\d+\b", P3)))
chk(">=30 unique test case IDs", len(tc_ids) >= 30, f"found {len(tc_ids)}: {tc_ids}")

# ---------- 6. no source code files ----------
print("\n[6] Workspace hygiene")
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
chk("only .md files in workspace -> docs/ contains Markdown only (specification purity)",
    not non_md, str(non_md))
missing = [f for f in SPEC_FILES if f not in tracked]
chk("only .md files in workspace -> all 6 specification files are tracked", not missing, str(missing))

print("\nRESULT:", "ALL CHECKS PASSED" if ok else "FAILURES PRESENT")
sys.exit(0 if ok else 1)
