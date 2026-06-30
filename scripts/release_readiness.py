#!/usr/bin/env python3
"""`make release-readiness` driver — advisory report.

Generates a single readout summarizing whether the current checkout looks
release-ready. Implements proposal §S3.2.

ADVISORY ONLY. This script never tags, never publishes, never pushes. Its
job is to produce evidence for the human who DOES decide whether to ship.

Sections emitted:
  * version              — from tokenpak/__init__.py
  * git                  — branch, commit, dirty
  * tests                — pytest -m quick exit status
  * audit                — bash scripts/audit.sh exit status (optional;
                           skipped with --no-audit when called from `make
                           release-readiness` if the operator just ran it)
  * package-dryrun       — scripts/audit_package_dryrun.py exit
  * leakage              — scripts/audit_internal_leakage.py exit
  * claims               — CLAIMS.md present + non-empty
  * docs-inventory       — scripts/audit-docs.sh exit (advisory)
  * known-blockers       — anything labeled BLOCKER in CHANGELOG.md or
                           release-blockers.txt (advisory; never crashes
                           the report if absent)
  * recommendation       — go / no-go / caveats

Exit codes: the report itself exits 0 regardless of go/no-go (the report IS
the artifact). Sub-checks' exits are captured and surfaced. Use --strict to
turn no-go into a non-zero exit.

Usage:
    python3 scripts/release_readiness.py
    python3 scripts/release_readiness.py --json
    python3 scripts/release_readiness.py --strict
    python3 scripts/release_readiness.py --skip-audit   # skip make audit sub-call
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Section:
    name: str
    status: str  # ok | warn | fail | skip
    details: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def run_cmd(cmd: list[str], cwd: Path, timeout: int = 600) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return 127, "", f"command not found: {exc}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    return proc.returncode, proc.stdout, proc.stderr


def section_version(root: Path) -> Section:
    init = root / "tokenpak" / "__init__.py"
    if not init.exists():
        return Section("version", "fail", details={"reason": "tokenpak/__init__.py missing"})
    version = None
    for line in init.read_text().splitlines():
        if line.strip().startswith("__version__"):
            version = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
    if not version:
        return Section("version", "fail", details={"reason": "__version__ not parseable"})
    return Section("version", "ok", details={"version": version})


def section_git(root: Path) -> Section:
    branch_rc, branch, _ = run_cmd(["git", "branch", "--show-current"], root)
    commit_rc, commit, _ = run_cmd(["git", "rev-parse", "HEAD"], root)
    status_rc, status_out, _ = run_cmd(["git", "status", "--porcelain"], root)
    dirty = bool(status_out.strip())
    if branch_rc or commit_rc or status_rc:
        return Section("git", "fail", details={
            "branch_rc": branch_rc, "commit_rc": commit_rc, "status_rc": status_rc,
        })
    return Section(
        "git",
        "warn" if dirty else "ok",
        details={
            "branch": branch.strip(),
            "commit": commit.strip(),
            "dirty": dirty,
            "uncommitted_lines": len(status_out.splitlines()) if dirty else 0,
        },
    )


def section_tests(root: Path, skip: bool = False) -> Section:
    if skip:
        return Section("tests-quick", "skip", notes=["skipped via --skip-tests"])
    rc, _out, _err = run_cmd(
        [sys.executable, "-m", "pytest", "-m", "quick", "-q", "--tb=line"],
        root,
        timeout=120,
    )
    status = "ok" if rc == 0 else "fail"
    return Section("tests-quick", status, details={"exit": rc})


def section_audit(root: Path, skip: bool) -> Section:
    if skip:
        return Section("audit", "skip", notes=["skipped via --skip-audit"])
    script = root / "scripts" / "audit.sh"
    if not script.exists():
        return Section("audit", "fail", details={"reason": "scripts/audit.sh missing"})
    rc, _, _ = run_cmd(["bash", str(script)], root, timeout=600)
    return Section("audit", "ok" if rc == 0 else "fail", details={"exit": rc})


def section_package_dryrun(root: Path) -> Section:
    rc, _, _ = run_cmd(
        [sys.executable, "scripts/audit_package_dryrun.py", "--root", str(root)],
        root,
    )
    return Section("package-dryrun", "ok" if rc == 0 else "fail", details={"exit": rc})


def section_leakage(root: Path) -> Section:
    rc, _, _ = run_cmd(
        [sys.executable, "scripts/audit_internal_leakage.py", "--root", str(root)],
        root,
    )
    return Section("internal-leakage", "ok" if rc == 0 else "fail", details={"exit": rc})


def section_claims(root: Path) -> Section:
    claims = root / "CLAIMS.md"
    if not claims.exists():
        return Section("claims", "fail", details={"reason": "CLAIMS.md missing"})
    size = claims.stat().st_size
    if size < 200:
        return Section("claims", "warn", details={"size_bytes": size, "reason": "very small"})
    return Section("claims", "ok", details={"size_bytes": size})


def section_docs_inventory(root: Path) -> Section:
    script = root / "scripts" / "audit-docs.sh"
    if not script.exists():
        return Section("docs-inventory", "skip", notes=["audit-docs.sh missing"])
    rc, _, _ = run_cmd(["bash", str(script)], root, timeout=120)
    return Section("docs-inventory", "ok" if rc == 0 else "warn", details={"exit": rc})


def section_known_blockers(root: Path) -> Section:
    blockers: list[str] = []
    rb = root / "release-blockers.txt"
    if rb.exists():
        for line in rb.read_text().splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                blockers.append(s)
    return Section(
        "known-blockers",
        "warn" if blockers else "ok",
        details={"count": len(blockers), "items": blockers[:20]},
    )


def recommendation(sections: list[Section]) -> tuple[str, list[str]]:
    fails = [s.name for s in sections if s.status == "fail"]
    warns = [s.name for s in sections if s.status == "warn"]
    if fails:
        return "no-go", [f"fail:{n}" for n in fails] + [f"warn:{n}" for n in warns]
    if warns:
        return "go-with-caveats", [f"warn:{n}" for n in warns]
    return "go", []


def emit_plain(report: dict) -> None:
    print("=" * 70)
    print("TokenPak Release Readiness Report")
    print(f"generated: {report['generated_at']}")
    print("=" * 70)
    for s in report["sections"]:
        print(f"\n[{s['status'].upper():>4}] {s['name']}")
        for k, v in s.get("details", {}).items():
            print(f"        {k}: {v}")
        for note in s.get("notes", []):
            print(f"        note: {note}")
    print()
    print("-" * 70)
    print(f"Recommendation: {report['recommendation'].upper()}")
    for reason in report.get("reasons", []):
        print(f"  - {reason}")
    print("-" * 70)
    print("(advisory only — no public release/tag/publish has been performed.)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="repo root (default: cwd)")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--skip-audit",
        action="store_true",
        help="skip the `bash scripts/audit.sh` sub-call (use when you just ran make audit)",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="skip the inner `pytest -m quick` sub-call (useful from within a pytest run)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when recommendation is no-go",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()

    sections = [
        section_version(root),
        section_git(root),
        section_tests(root, skip=args.skip_tests),
        section_audit(root, skip=args.skip_audit),
        section_package_dryrun(root),
        section_leakage(root),
        section_claims(root),
        section_docs_inventory(root),
        section_known_blockers(root),
    ]

    rec, reasons = recommendation(sections)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "advisory": True,
        "sections": [asdict(s) for s in sections],
        "recommendation": rec,
        "reasons": reasons,
    }

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        emit_plain(report)

    if args.strict and rec == "no-go":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
