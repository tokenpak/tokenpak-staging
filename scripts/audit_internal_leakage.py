#!/usr/bin/env python3
"""Public-safety leakage detector for the public tokenpak tree.

Scans the shipped public surface for structural private-path and internal-ID
shapes. Broader full-tree release-gate scanning lives in
``scripts/release_gate/public_safety_scan.py``; this command stays narrow so
``make audit`` remains a deterministic local developer check.

Exit codes:
    0 — clean
    1 — at least one leak detected
    2 — invocation error (not a git repo / git missing)

Usage:
    python3 scripts/audit_internal_leakage.py [--root PATH] [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

LEAK_PATTERNS = [
    (
        r"/(?:home|Users)/(?!<user>(?:/|$)|user(?:/|$)|runner(?:/|$)|workspace(?:/|$)|actions(?:/|$)|tmp(?:/|$))"
        r"[A-Za-z0-9._-]+/",
        "private-home-path",
    ),
    (
        r"(?:~|/(?:home|Users)/[A-Za-z0-9._-]+)?/vault/[0-9]{2}_[A-Z][A-Z0-9_]+",
        "numbered-vault-path",
    ),
    (
        r"\b(?:tracked|ticket|task|initiative|packet|follow-up|work item|internal id|reference)\s+"
        r"(?:in|as|id|ref|refs|#)?\s*[:#]?\s*"
        r"[A-Z]{2,8}[0-9]?-(?=[A-Z0-9-]*[0-9])[A-Z0-9][A-Z0-9-]*\b",
        "internal-task-id-shape",
    ),
]

SCAN_EXTENSIONS = {
    ".cfg", ".ini", ".json", ".md", ".py", ".rst", ".sh", ".toml", ".txt",
    ".yaml", ".yml",
}

SHIPPED_INCLUDE_PREFIXES = ("tokenpak/",)
SHIPPED_INCLUDE_ROOT_FILES = ("README.md", "CLAIMS.md", "pyproject.toml", "LICENSE")

EXCLUDED_PATH_PREFIXES = (
    "tokenpak/integrations/openclaw/",
    "tokenpak/creds/providers/openclaw.py",
    "tokenpak/services/routing_service/platform_bridge.py",
    "tokenpak/tests/",
)


def list_tracked_files(root: Path) -> list[Path]:
    try:
        out = subprocess.check_output(["git", "ls-files"], cwd=root, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"error: git ls-files failed: {exc}", file=sys.stderr)
        sys.exit(2)
    return [root / line for line in out.splitlines() if line.strip()]


def scan_file(path: Path, compiled: list[tuple[re.Pattern[str], str]]) -> list[dict]:
    findings: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings
    for lineno, line in enumerate(text.splitlines(), start=1):
        for regex, kind in compiled:
            if regex.search(line):
                findings.append(
                    {
                        "file": str(path),
                        "line": lineno,
                        "kind": kind,
                        "match": line.strip()[:200],
                    }
                )
    return findings


def _finding_payload(root: Path) -> list[dict]:
    compiled = [(re.compile(p), k) for p, k in LEAK_PATTERNS]
    all_findings: list[dict] = []

    for path in list_tracked_files(root):
        rel = path.relative_to(root).as_posix()
        if any(rel.startswith(prefix) for prefix in EXCLUDED_PATH_PREFIXES):
            continue
        in_shipped_pkg = any(rel.startswith(p) for p in SHIPPED_INCLUDE_PREFIXES)
        in_root_metadata = rel in SHIPPED_INCLUDE_ROOT_FILES
        if not (in_shipped_pkg or in_root_metadata):
            continue
        if path.suffix not in SCAN_EXTENSIONS:
            continue
        if not path.is_file():
            continue
        all_findings.extend(scan_file(path, compiled))

    return all_findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="repo root (default: cwd)")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not (root / ".git").exists():
        print(f"error: {root} is not a git repo", file=sys.stderr)
        return 2

    all_findings = _finding_payload(root)

    if args.json:
        json.dump(
            {"ok": not all_findings, "findings": all_findings},
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
    else:
        if all_findings:
            print(f"FAIL: {len(all_findings)} internal-leak finding(s):")
            for f in all_findings[:50]:
                print(f"  {f['file']}:{f['line']}  [{f['kind']}]  {f['match']}")
            if len(all_findings) > 50:
                print(f"  ...and {len(all_findings) - 50} more")
        else:
            print("OK: no internal leakage detected in tracked files.")

    return 1 if all_findings else 0


if __name__ == "__main__":
    sys.exit(main())
