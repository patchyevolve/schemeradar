"""Specification consistency suite — CI entry point.

    python -m tools.validate_specs              # all phases
    python -m tools.validate_specs --phase 3    # one phase (or: --phase phase3)
    python -m tools.validate_specs --list

BUILD_ORDER Step 1, task 1.8: "Add the specification validators to CI (run the
Phase 2/3 checks on every push) — keeps docs and code from drifting."

Each phase script under tools/spec_checks/ is a self-contained checker:

    phase2.py  JSON Schema + V-CF cross-field invariants + both worked examples
    phase3.py  workflow structure, Edge Case A/B/C arithmetic, 50 test IDs, I-1..I-10
    phase4.py  9 slides, speaker notes, 180-second pitch, cross-document numbers
    phase5.py  README/BUILD_ORDER structure + every phase re-run (nesting guard)

Exit code is non-zero if any selected phase fails, so a single invocation is
CI-safe.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

CHECKS_DIR = pathlib.Path(__file__).resolve().parent / "spec_checks"
PHASES = ("phase2", "phase3", "phase4", "phase5")

TITLES = {
    "phase2": "Data Spec — schema, invariants, worked examples",
    "phase3": "Workflow & Tests — edge cases, 50 tests, 10 invariants",
    "phase4": "Pitch deck — slides, notes, 180 s script, numeric consistency",
    "phase5": "Suite — README / BUILD_ORDER + nested re-run of phases 2-4",
}


def run(phase: str) -> bool:
    script = CHECKS_DIR / f"{phase}.py"
    if not script.exists():
        print(f"  MISSING {script}", file=sys.stderr)
        return False
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    tail = [ln for ln in (proc.stdout + proc.stderr).splitlines() if ln.strip()]
    summary = tail[-1] if tail else "(no output)"
    marker = "ALL CHECKS PASSED" in proc.stdout
    ok = proc.returncode == 0 and marker
    print(f"  {'PASS' if ok else 'FAIL'}  {phase}  — {TITLES[phase]}")
    if not ok:
        for line in [ln for ln in tail if "FAIL" in ln][:8]:
            print(f"        {line.strip()}")
        print(f"        {summary}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the SchemeRadar specification validators.")
    parser.add_argument("--phase", default="all",
                        help="all (default), or a phase as 2/3/4/5 or phase2..phase5")
    parser.add_argument("--list", action="store_true", help="list available phases and exit")
    args = parser.parse_args()

    # Accept both `--phase 3` and `--phase phase3`.
    phase_arg = args.phase
    if phase_arg.isdigit():
        phase_arg = f"phase{phase_arg}"
    if phase_arg != "all" and phase_arg not in PHASES:
        parser.error(f"--phase: invalid choice: {args.phase!r} "
                     f"(choose from {', '.join([*PHASES, 'all'])})")

    if args.list:
        for p in PHASES:
            print(f"{p:8s} {TITLES[p]}")
        return 0

    selected = PHASES if phase_arg == "all" else (phase_arg,)
    print("SchemeRadar specification validators\n" + "-" * 44)
    results = {p: run(p) for p in selected}

    passed = sum(results.values())
    total = len(results)
    print("-" * 44)
    print(f"{passed}/{total} phases passed")
    if passed != total:
        print("RESULT: FAILURES PRESENT")
        return 1
    print("RESULT: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
