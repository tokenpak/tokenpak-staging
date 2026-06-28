#!/usr/bin/env python3
"""Package-contents dry-run validation for tokenpak.

Verifies that what the build backend WOULD ship is structurally sound, without
actually building wheels/sdists. Checks:

  * `tokenpak` package importable
  * `tokenpak.__version__` is set and non-empty
  * Key CLI entrypoints importable (tokenpak.cli)
  * Top-level metadata files present (README.md, LICENSE, pyproject.toml)
  * Mandatory package data directories present (tokenpak/, tokenpak/cli/)

Scope note: this script validates the IN-TREE package state on the workbench.
It does NOT enforce the public-mirror's "forbidden top-level path" list —
that is the job of `.github/workflows/public-layout-check.yml`, which runs
on the public mirror where local-only directories like `.tokenpak/` or
`.benchmarks/` don't exist. The `audit_internal_leakage.py` companion script
catches leak patterns inside TRACKED file contents.

Exit codes:
    0 — all checks pass
    1 — at least one check failed

Usage:
    python3 scripts/audit_package_dryrun.py [--root PATH] [--json]
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

REQUIRED_TOP_LEVEL = ["README.md", "LICENSE", "pyproject.toml"]
REQUIRED_DIRS = ["tokenpak", "tokenpak/cli"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="repo root (default: cwd)")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    failures: list[dict] = []
    notes: list[str] = []

    for f in REQUIRED_TOP_LEVEL:
        if not (root / f).exists():
            failures.append({"check": "required-top-level", "missing": f})

    for d in REQUIRED_DIRS:
        if not (root / d).is_dir():
            failures.append({"check": "required-dir", "missing": d})

    sys.path.insert(0, str(root))
    try:
        tokenpak = importlib.import_module("tokenpak")
        version = getattr(tokenpak, "__version__", None)
        if not version:
            failures.append({"check": "version", "reason": "tokenpak.__version__ missing or empty"})
        else:
            notes.append(f"tokenpak.__version__={version}")
    except Exception as exc:
        failures.append({"check": "import-tokenpak", "reason": str(exc)})

    try:
        importlib.import_module("tokenpak.cli")
        notes.append("tokenpak.cli importable")
    except Exception as exc:
        failures.append({"check": "import-cli", "reason": str(exc)})

    result = {
        "ok": not failures,
        "failures": failures,
        "notes": notes,
    }

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        if failures:
            print(f"FAIL: package dry-run found {len(failures)} issue(s):")
            for f in failures:
                print(f"  {f}")
        else:
            print("OK: package dry-run validation passed.")
            for n in notes:
                print(f"  {n}")

    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
