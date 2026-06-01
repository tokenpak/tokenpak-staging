#!/usr/bin/env python3
"""Internal-docs / private-paths leakage detector for the public tokenpak tree.

Scans tracked source files for substrings that should never ship publicly:
internal home directories, vault path prefixes, deprecated personal repo
references, and obvious internal task-ID prefixes. Pure local check —
no network, no provider calls.

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
    (r"/home/(sue|trix|cali|aya|suki|dee|reipo)/", "internal-home-path"),
    (r"kaywhy331/", "deprecated-personal-repo"),
    # NOTE: do NOT match a bare `/vault/` — the OSS package ships a public
    # vault feature whose default store is `~/vault/.tokenpak/`. Only flag
    # references that target the internal numbered-tree layout
    # (vault/01_PROJECTS, vault/03_AGENT_PACKS, etc.).
    (r"/vault/\d\d_[A-Z]", "internal-vault-path"),
    (r"\b(03_AGENT_PACKS|02_COMMAND_CENTER|06_RUNTIME)\b", "vault-tree-ref"),
    (r"\b(PMGTM-\d+|TRIX-\d+|CALI-\d+|SUKI-\d+)\b", "internal-task-id"),
]

SCAN_EXTENSIONS = {
    ".py", ".md", ".txt", ".yml", ".yaml", ".json", ".toml",
    ".sh", ".cfg", ".ini", ".rst",
}

# Default scan scope is the SHIPPED public surface: the importable Python
# package + root-level metadata files. Internal docs, build tooling, and
# .github/ workflows (which legitimately contain leak-pattern regexes as
# their own input) are excluded. Override with --all to scan the entire tree.
SHIPPED_INCLUDE_PREFIXES = (
    "tokenpak/",
)
SHIPPED_INCLUDE_ROOT_FILES = (
    "README.md",
    "CLAIMS.md",
    "pyproject.toml",
    "LICENSE",
)
EXCLUDED_PATH_PREFIXES = (
    "tests/",
    "scripts/audit_internal_leakage.py",
    "CHANGELOG.md",
    "docs/",
    "examples/",
    "scripts/",
    ".github/",
    "packages/",
    "recipes/",
    "schemas/",
    "config/",
    "docs_site/",
    "build/",
    "dist/",
)


def list_tracked_files(root: Path) -> list[Path]:
    try:
        out = subprocess.check_output(
            ["git", "ls-files"], cwd=root, text=True
        )
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
                findings.append({
                    "file": str(path),
                    "line": lineno,
                    "kind": kind,
                    "match": line.strip()[:200],
                })
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="repo root (default: cwd)")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not (root / ".git").exists():
        print(f"error: {root} is not a git repo", file=sys.stderr)
        return 2

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
