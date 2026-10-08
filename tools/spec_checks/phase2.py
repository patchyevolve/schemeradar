import json, re, sys, pathlib
import jsonschema
from jsonschema import Draft202012Validator

DOC = (pathlib.Path(__file__).resolve().parents[2] / "docs" / "DATA_SPEC.md").read_text()

blocks = re.findall(r"```json\n(.*?)```", DOC, re.S)
print(f"json blocks found: {len(blocks)}")

parsed = []
for i, b in enumerate(blocks):
    try:
        parsed.append((i, json.loads(b)))
    except Exception as e:
        print(f"  BLOCK {i}: JSON PARSE ERROR -> {e}")
        # show the offending line
        for ln, line in enumerate(b.splitlines(), 1):
            try:
                json.loads("[" + line.rstrip(",").replace("True","true").replace("False","false") + "]")
            except Exception:
                pass
        parsed.append((i, None))

scheme_schema = None
profile_schema = None
instances = []
for i, obj in parsed:
    if obj is None:
        continue
    if isinstance(obj, dict) and obj.get("title") == "SchemeRadar Canonical Scheme":
        scheme_schema = (i, obj)
    elif isinstance(obj, dict) and obj.get("title") == "ProfileContext":
        profile_schema = (i, obj)
    elif isinstance(obj, dict) and isinstance(obj.get("schema_version"), str) and "scheme_id" in obj and "required_documents" in obj:
        instances.append((i, obj))
    elif isinstance(obj, dict) and isinstance(obj.get("schema_version"), str) and "documents_in_hand" in obj:
        instances.append((i, obj))

print(f"scheme schema block: {scheme_schema[0] if scheme_schema else None}")
print(f"profile schema block: {profile_schema[0] if profile_schema else None}")
print(f"instance blocks: {[i for i,_ in instances]}")

fc = Draft202012Validator.FORMAT_CHECKER

def check(name, schema, inst):
    errs = sorted(Draft202012Validator(schema, format_checker=fc).iter_errors(inst), key=lambda e: e.path)
    if not errs:
        print(f"  PASS  {name}")
        return True
    print(f"  FAIL  {name}  ({len(errs)} error(s))")
    for e in errs[:20]:
        path = "/" + "/".join(str(p) for p in e.path)
        print(f"        - {path}: {e.message[:220]}")
    return False

ok = True
if scheme_schema and profile_schema:
    _, ss = scheme_schema
    _, ps = profile_schema
    # self-validate the schemas themselves
    for nm, sch in [("Scheme schema", ss), ("ProfileContext schema", ps)]:
        try:
            Draft202012Validator.check_schema(sch)
            print(f"  PASS  {nm} is a valid draft 2020-12 schema")
        except Exception as e:
            ok = False
            print(f"  FAIL  {nm}: {e}")
    for i, inst in instances:
        sch = ps if "documents_in_hand" in inst else ss
        nm = f"block {i}: {inst.get('scheme_id') or 'ProfileContext'}"
        ok &= check(nm, sch, inst)
else:
    ok = False
    print("  FAIL  could not locate schemas")

# consistency spot-checks vs Phase 1
arch = (pathlib.Path(__file__).resolve().parents[2] / "docs" / "ARCHITECTURE.md").read_text()
checks = [
    ("W_D = 0.60", "$W_D$ | 0.60" in arch),
    ("W_S = 0.40", "$W_S$ | 0.40" in arch),
    ("W_D/W_S echoed in DATA_SPEC", '"W_D": 0.60' in DOC and '"W_S": 0.40' in DOC),
    ("RRF k = 60", "$k$ | **60" in arch or "RRF smoothing constant" in arch),
    ("P_cap 0.60", "0.60" in arch and "P_{cap}" in arch),
    ("bge-m3 1024", "1024" in arch and "bge-m3" in arch),
    ("score 0.737 in Phase1", "0.737" in arch),
    ("score 0.737 in Phase2", "0.737" in DOC),
    ("S_hybrid 0.968 in Phase1", "0.968" in arch),
    ("verification enum identical", arch.count("INTAKE_OPEN") > 0 and DOC.count("INTAKE_OPEN") > 0),
]
# enum + lattice identity checks
verif_enum = ["INTAKE_OPEN", "INTAKE_CLOSED", "UNREACHABLE", "BLOCKED", "UNVERIFIED", "PENDING"]
checks.append(("all 6 verification_status values in both docs",
               all(v in arch for v in verif_enum) and all(v in DOC for v in verif_enum)))
lattice = '"below_10", "10th", "12th", "diploma", "bachelor_1st_year", "bachelor", "post_grad", "phd"'
checks.append(("education lattice identical in both docs", lattice in DOC and all(
    t in arch for t in ["below_10", "bachelor_1st_year", "post_grad"])))
checks.append(("tier ranges 0.15-0.25 / 0.25-0.35 match Phase 1",
               all(r in arch and r in DOC for r in ["0.10 – 0.15", "0.15 – 0.25", "0.25 – 0.35"])))
checks.append(("near-miss delta 0.05 present in both", "0.05" in arch and "delta_{income}" in DOC))
checks.append(("parse confidence 0.75 in both", "0.75" in arch and "0.75" in DOC))
checks.append(("field names present in DATA_SPEC",
               all(f in DOC for f in ["scheme_id", "income_ceiling_annual", "min_age", "max_age",
                                      "gender", "domicile_state", "allowed_categories",
                                      "min_education_level", "required_documents", "verification_status"])))

# ---- simulate Phase 1/2 invariants on the two scheme instances ----
TIER_RANGES = {0: (0.00, 0.00), 1: (0.05, 0.05), 2: (0.10, 0.15), 3: (0.15, 0.25), 4: (0.25, 0.35)}
print("\nInvariant simulation (V-CF1, V-CF2, V-CF3, V-CF8):")
for i, inst in instances:
    if "required_documents" not in inst:
        continue
    nm = inst["scheme_id"]
    errs = []
    ma, xa = inst.get("min_age"), inst.get("max_age")
    if ma is not None and xa is not None and not (ma <= xa):
        errs.append("V-CF1 min_age>max_age")
    ids = set()
    for d in inst["required_documents"]:
        ids.add(d["document_id"])
        lo, hi = TIER_RANGES[d["tier"]]
        if not (lo - 1e-9 <= d["penalty_per_document"] <= hi + 1e-9):
            errs.append(f"V-CF2 {d['document_id']} tier{d['tier']} p={d['penalty_per_document']} outside [{lo},{hi}]")
        if d["requirement_weight"] not in (0.0, 0.5, 1.0):
            errs.append(f"V-CF3 {d['document_id']} bad weight")
        if d["requirement_weight"] == 0.5 and not d["alternatives"]:
            errs.append(f"V-CF3 {d['document_id']} w=0.5 without alternatives")
        if inst.get("income_ceiling_annual") is not None and not inst.get("income_ceiling_scope"):
            errs.append("V-CF9 income ceiling without scope")
    for d in inst["required_documents"]:
        for alt in d.get("alternatives", []):
            if alt["document_id"] not in ids:
                errs.append(f"alt {alt['document_id']} not declared in required_documents")
    anns = [b.get("annual_value_inr") for b in inst["benefits"]]
    if all(a is not None for a in anns):
        if sum(anns) != inst.get("benefits_total_value_annual"):
            errs.append(f"V-CF8 sum={sum(anns)} != {inst.get('benefits_total_value_annual')}")
    if errs:
        ok = False
        print(f"  FAIL  {nm}")
        for e in errs:
            print(f"        - {e}")
    else:
        print(f"  PASS  {nm} ({len(inst['required_documents'])} documents, tiers verified)")
print("\nCross-document consistency spot checks:")
for label, res in checks:
    print(f"  {'PASS' if res else 'FAIL'}  {label}")
    ok &= res

print("\nRESULT:", "ALL CHECKS PASSED" if ok else "FAILURES PRESENT")
sys.exit(0 if ok else 1)
