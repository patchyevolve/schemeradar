"""Validate the canonical seed instances (DATA_SPEC 4.1 / 5.1).

BUILD_ORDER 4.5 validation command #1 and DoD checkbox #1:

    python -m tools.validate_examples

Layer L1 (JSON Schema, draft 2020-12) followed by layer L3 (V-CF1..V-CF12,
DATA_SPEC 2.4). JSON Schema cannot express the cross-field rules, so every one
of the 12 is evaluated here explicitly -- none are skipped silently.

Two invariants have an out-of-band component that lives below this script:
  V-CF6 also needs the unique index on `schemes`/`scheme_revisions` (task 2.3),
        and `scheme_revisions` is not in the seed set, so uniqueness is checked
        across whatever is present here plus asserted by the DB index.
  V-CF7 also needs a re-check against the resolved DNS answer at request time
        (Step 3 SSRF guard); only the static allowlist pattern is checked here.
"""
import json
import pathlib
import re
import sys

import jsonschema
from jsonschema import Draft202012Validator

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "scheme.schema.json"
SEED_DIR = ROOT / "seeds"

# DATA_SPEC 2.5 tier -> (lo, hi) inclusive.
TIER_RANGES = {0: (0.00, 0.00), 1: (0.05, 0.05), 2: (0.10, 0.15), 3: (0.15, 0.25), 4: (0.25, 0.35)}

# DATA_SPEC 2.2: the gate field set ("Scheme.gates" is flat at the top level).
GATE_FIELDS = {
    "min_age", "max_age", "gender", "domicile_state", "income_ceiling_annual",
    "income_ceiling_scope", "allowed_categories", "requires_minority_flag",
    "min_education_level", "allowed_occupations",
}

# DATA_SPEC 2.4 / 2.5: `unit` -> required numeric kind for `value`.
NUMERIC_UNITS = {"hectare", "percent", "year"}   # number
INTEGER_UNITS = {"person"}                       # integer

GOV_HOST_PATTERN = r"^https://[^/]+\.(gov\.in|nic\.in)(:\d+)?(/.*)?$"


def load() -> tuple[dict, list[dict]]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    instances = []
    for f in sorted(SEED_DIR.glob("*.json")):
        instances.append((f, json.loads(f.read_text(encoding="utf-8"))))
    return schema, instances


def main() -> int:
    schema, instances = load()
    ok = True

    if not instances:
        print("FAIL  no seed instances found under seeds/")
        return 1

    print(f"Validating {len(instances)} instance(s) against {SCHEMA_PATH.name}\n")

    # ---- L1: JSON Schema -------------------------------------------------
    print("[L1] JSON Schema (draft 2020-12)")
    Draft202012Validator.check_schema(schema)
    print("  PASS  schema itself is a valid draft 2020-12 schema")
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    for path, inst in instances:
        errs = sorted(validator.iter_errors(inst), key=lambda e: list(e.path))
        if errs:
            ok = False
            print(f"  FAIL  {path.name} ({len(errs)} error(s))")
            for e in errs[:20]:
                at = "/" + "/".join(str(p) for p in e.path)
                print(f"        - {at}: {e.message[:200]}")
        else:
            print(f"  PASS  {path.name}")

    # ---- L3: cross-field invariants -------------------------------------
    print("\n[L3] Cross-field invariants V-CF1..V-CF12")

    # V-CF6 needs a set-wide view; collect first.
    ids = [i.get("scheme_id") for _, i in instances]
    slugs = [i.get("slug") for _, i in instances]

    results: dict[str, list[str]] = {f"V-CF{n}": [] for n in range(1, 13)}

    for path, inst in instances:
        nm = path.name
        sid = inst.get("scheme_id")

        # V-CF1 age ordering
        ma, xa = inst.get("min_age"), inst.get("max_age")
        if ma is not None and xa is not None and not (ma <= xa):
            results["V-CF1"].append(f"{nm}: min_age {ma} > max_age {xa}")

        # V-CF2 tier ranges
        for d in inst.get("required_documents", []):
            t, p = d.get("tier"), d.get("penalty_per_document")
            if t not in TIER_RANGES:
                results["V-CF2"].append(f"{nm}: {d.get('document_id')} unknown tier {t}")
                continue
            lo, hi = TIER_RANGES[t]
            if not (lo - 1e-9 <= float(p) <= hi + 1e-9):
                results["V-CF2"].append(f"{nm}: {d.get('document_id')} tier {t} p={p} outside [{lo}, {hi}]")

        # V-CF3 weight domain + alternatives requirement
        for d in inst.get("required_documents", []):
            w = d.get("requirement_weight")
            if w not in (0.0, 0.5, 1.0):
                results["V-CF3"].append(f"{nm}: {d.get('document_id')} weight {w} not in {{0.0,0.5,1.0}}")
            if w == 0.5 and not d.get("alternatives"):
                results["V-CF3"].append(f"{nm}: {d.get('document_id')} weight 0.5 without alternatives")
            declared = {x["document_id"] for x in inst.get("required_documents", [])}
            for alt in d.get("alternatives", []):
                if alt.get("document_id") not in declared:
                    results["V-CF3"].append(f"{nm}: alternative {alt.get('document_id')} not declared")

        # V-CF4 intake window vs verification_status
        vs = inst.get("verification_status")
        iw = inst.get("intake_window") or {}
        cs = iw.get("cycle_status")
        if vs == "INTAKE_OPEN" and cs not in ("open", "unknown"):
            results["V-CF4"].append(f"{nm}: INTAKE_OPEN but cycle_status={cs!r}")
        if vs == "INTAKE_CLOSED" and iw.get("closes_on") is None:
            results["V-CF4"].append(f"{nm}: INTAKE_CLOSED but closes_on is null")

        # V-CF5 verified_at required for an asserted live status
        if vs in ("INTAKE_OPEN", "INTAKE_CLOSED") and inst.get("verified_at") is None:
            results["V-CF5"].append(f"{nm}: {vs} but verified_at is null")

        # V-CF7 allowlist (static half; DNS re-check happens in Step 3)
        for key in ("portal_url", "guide_url"):
            u = inst.get(key)
            if u and re.match(GOV_HOST_PATTERN, u) is None:
                results["V-CF7"].append(f"{nm}: {key} {u!r} not in gov.in/nic.in allowlist")

        # V-CF8 benefits sum
        anns = [b.get("annual_value_inr") for b in inst.get("benefits", [])]
        if anns and all(a is not None for a in anns):
            total = sum(anns)
            if total != inst.get("benefits_total_value_annual"):
                results["V-CF8"].append(f"{nm}: sum {total} != benefits_total_value_annual {inst.get('benefits_total_value_annual')}")

        # V-CF9 ceiling implies scope
        if inst.get("income_ceiling_annual") is not None and not inst.get("income_ceiling_scope"):
            results["V-CF9"].append(f"{nm}: income_ceiling_annual without income_ceiling_scope")
        if inst.get("income_ceiling_annual") is None and inst.get("income_ceiling_scope") is not None:
            results["V-CF9"].append(f"{nm}: income_ceiling_scope without income_ceiling_annual")

        # V-CF10 override `set` keys subset of the gate field set
        for ov in inst.get("conditional_gate_overrides", []):
            bad = set(ov.get("set", {})) - GATE_FIELDS
            if bad:
                results["V-CF10"].append(f"{nm}: {ov.get('override_id')} sets non-gate keys {sorted(bad)}")

        # V-CF11 domain gate value type vs unit
        for g in inst.get("domain_gates", []):
            unit, val = g.get("unit"), g.get("value")
            rid = g.get("rule_id")
            # `value` may also be a profile expression string; the schema
            # already constrains that case, so only literal numbers are typed.
            if isinstance(val, bool):
                results["V-CF11"].append(f"{nm}: {rid} unit={unit} but value {val!r} is a boolean")
            elif isinstance(val, (int, float)) and unit in INTEGER_UNITS:
                if not float(val).is_integer():
                    results["V-CF11"].append(f"{nm}: {rid} unit=person but value {val!r} is not an integer")

        # V-CF12 confidence floor for the indexed state
        if inst.get("index_state") == "indexed":
            pc = (inst.get("parse") or {}).get("parse_confidence")
            if pc is None or float(pc) < 0.75:
                results["V-CF12"].append(f"{nm}: index_state=indexed but parse_confidence={pc} < 0.75")

    # V-CF6 set-wide uniqueness
    for kind, values in (("scheme_id", ids), ("slug", slugs)):
        dupes = {v for v in values if values.count(v) > 1}
        if dupes:
            results["V-CF6"].append(f"duplicate {kind} in seed set: {sorted(dupes)}")

    notes = {
        "V-CF6": "set-level check here + unique index enforced by task 2.3",
        "V-CF7": "static allowlist here + resolved-DNS re-check in Step 3",
    }
    for vid in sorted(results, key=lambda v: int(v.split("CF")[1])):
        errs = results[vid]
        if errs:
            ok = False
            print(f"  FAIL  {vid}")
            for e in errs:
                print(f"        - {e}")
        else:
            suffix = f"  ({notes[vid]})" if vid in notes else ""
            print(f"  PASS  {vid}{suffix}")

    print("\nRESULT:", "ALL CHECKS PASSED" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
