"""Re-pin the direct dependencies in services/api/requirements.txt.

Usage:
    python -m tools.pin_requirements [--check]

Reads the current virtualenv, resolves the installed version of every direct
dependency named in services/api/requirements.txt, and rewrites those pins
in place. Section headers, comments and extras (``uvicorn[standard]``) are
preserved so the file stays human-readable.

    --check   do not write; exit non-zero if any pin has drifted (CI mode)
"""

from __future__ import annotations

import argparse
import re
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO_ROOT / "services" / "api" / "requirements.txt"

# `fastapi==1.2.3` or `uvicorn[standard]==1.2.3`
PIN_RE = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)(?P<extras>\[[^\]]*\])?==(?P<pin>[^\s#]+)")


def direct_names() -> list[tuple[str, str, str]]:
    """Return (line_prefix, distribution_name, current_pin) for each pin."""
    found: list[tuple[str, str, str]] = []
    for line in REQUIREMENTS.read_text().splitlines():
        m = PIN_RE.match(line.strip())
        if m:
            found.append(
                (
                    f"{m.group('name')}{m.group('extras') or ''}",
                    m.group("name"),
                    m.group("pin"),
                )
            )
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify pins only; never write")
    args = parser.parse_args()

    if not REQUIREMENTS.exists():
        print(f"missing {REQUIREMENTS}", file=sys.stderr)
        return 2

    text = REQUIREMENTS.read_text()
    drift: list[str] = []
    rewritten = text

    for prefix, name, current in direct_names():
        try:
            installed = version(name)
        except PackageNotFoundError:
            drift.append(f"{name}: not installed (expected {current})")
            continue
        if installed != current:
            drift.append(f"{name}: pinned {current} but installed {installed}")
            rewritten = re.sub(
                rf"^({re.escape(prefix)}==)[^\s#]+",
                rf"\g<1>{installed}",
                rewritten,
                flags=re.MULTILINE,
            )

    if args.check:
        if drift:
            print("dependency pin drift detected:", file=sys.stderr)
            for d in drift:
                print(f"  - {d}", file=sys.stderr)
            return 1
        print(f"all {len(direct_names())} direct dependency pins match the environment")
        return 0

    if drift:
        REQUIREMENTS.write_text(rewritten)
        print(f"re-pinned {len(drift)} direct dependency pin(s):")
        for d in drift:
            print(f"  - {d}")
    else:
        print(f"no drift; {len(direct_names())} pins already match the environment")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
