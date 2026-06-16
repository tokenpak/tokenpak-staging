#!/usr/bin/env python3
"""check_public_conformance.py — public-surface conformance gate (delta mode).

Advisory orchestrator that scans changed files for public-surface policy
violations. Pattern lists live as curated plaintext under
``scripts/release_gate/conformance/`` (one entry per line; ``#`` and blank
lines ignored) so they can be amended without code changes and are not
themselves scanned as source.

Check classes (the delta-checkable subset; trust-files presence/lint and
cross-surface install/version consistency are separate later PRs):

  marketing  — forbidden marketing phrases (messaging & positioning policy)
  privacy    — banned absolute privacy claims (privacy & data policy)
  internal   — internal identity / private-path / credential / task-ID
               leaks (public-safe-defaults leak register; regex list)
  pyproject  — author/maintainer is not a personal name (leak register
               applied to [project.authors]/[project.maintainers])

Modes:
  --mode delta     (default) scan only the changed files supplied via
                   ``--changed-files-from FILE`` or ``--stdin``; mirrors the
                   delta scope of the identity scan.
  --mode baseline  reserved for the RC pre-promotion full-tree job; not
                   wired in this advisory build (no-op).

Rollout is ADVISORY: findings print as GitHub ``::warning`` annotations and
the process exits 0. ``--enforce`` (reserved for the governed enforce-flip,
never set in CI this cycle) makes any finding exit non-zero.

Usage:
    python3 scripts/release_gate/check_public_conformance.py \\
        --mode delta --changed-files-from changed-files.txt
    git diff --name-only BASE HEAD | \\
        python3 scripts/release_gate/check_public_conformance.py --stdin
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

CONF_DIR = Path(__file__).resolve().parent / "conformance"

# Surfaces whose own content must not be scanned: the pattern registers hold
# the very tokens they forbid, and tests carry deliberate trigger fixtures.
EXCLUDE_EXACT = frozenset(
    {
        "scripts/release_gate/check_public_conformance.py",
        ".github/workflows/public-conformance-check.yml",
        ".github/workflows/identity-language-check.yml",
    }
)
EXCLUDE_PREFIXES = (
    "scripts/release_gate/conformance/",
    "tests/",
)
TEXT_SUFFIXES = frozenset(
    {
        ".md", ".py", ".txt", ".toml", ".yml", ".yaml", ".json",
        ".rst", ".cfg", ".ini", ".sh", ".js", ".ts", ".tsx",
    }
)


def load_list(name: str) -> list[str]:
    """Read one curated register; drop comment and blank lines."""
    out: list[str] = []
    for line in (CONF_DIR / name).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def load_patterns():
    """Return (marketing_phrases, privacy_phrases, compiled_internal_regexes)."""
    marketing = load_list("forbidden_marketing_phrases.txt")
    privacy = load_list("banned_privacy_claims.txt")
    internal = [re.compile(p) for p in load_list("forbidden_internal_patterns.txt")]
    return marketing, privacy, internal


def is_excluded(rel: str) -> bool:
    return rel in EXCLUDE_EXACT or any(rel.startswith(p) for p in EXCLUDE_PREFIXES)


def scan_text(rel_path, text, marketing, privacy, internal):
    """Yield (path, line_no, class, matched) tuples for one file's content."""
    findings = []
    for i, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        for phrase in marketing:
            if phrase.lower() in low:
                findings.append((rel_path, i, "marketing", phrase))
        for phrase in privacy:
            if phrase.lower() in low:
                findings.append((rel_path, i, "privacy", phrase))
        for rx in internal:
            if rx.search(line):
                findings.append((rel_path, i, "internal", rx.pattern))
    return findings


def scan_pyproject(rel_path, text, internal):
    """Flag a personal name (any leak-register hit) inside the author or
    maintainer tables. Robust personal-name heuristics are a later tier; for
    now the register is the proxy (it carries the known private identities)."""
    findings = []
    in_block = False
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith(("[project.authors]", "[project.maintainers]")) or s in (
            "authors = [",
            "maintainers = [",
        ):
            in_block = True
            continue
        if in_block:
            if s.startswith("["):
                in_block = False
            else:
                for rx in internal:
                    if rx.search(line):
                        findings.append((rel_path, i, "pyproject", rx.pattern))
                if "]" in s:
                    in_block = False
    return findings


def collect_files(args) -> list[str]:
    if args.changed_files_from:
        raw = Path(args.changed_files_from).read_text(encoding="utf-8")
    elif args.stdin:
        raw = sys.stdin.read()
    else:
        raw = ""
    return [f for f in (x.strip() for x in raw.split()) if f]


def run_delta(files, root):
    marketing, privacy, internal = load_patterns()
    root = Path(root)
    findings = []
    scanned = 0
    for rel in files:
        if is_excluded(rel) or Path(rel).suffix not in TEXT_SUFFIXES:
            continue
        fp = root / rel
        if not fp.is_file():
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        findings.extend(scan_text(rel, text, marketing, privacy, internal))
        if Path(rel).name == "pyproject.toml":
            findings.extend(scan_pyproject(rel, text, internal))
    return scanned, findings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="public-surface conformance gate")
    ap.add_argument("--mode", choices=["delta", "baseline"], default="delta")
    ap.add_argument("--changed-files-from", metavar="FILE")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--root", default=".")
    ap.add_argument("--enforce", action="store_true")
    args = ap.parse_args(argv)

    if args.mode == "baseline":
        print(
            "public-conformance: baseline (full-tree) mode is reserved for the "
            "RC pre-promotion job and is not wired in this advisory build; no-op.",
            file=sys.stderr,
        )
        return 0

    scanned, findings = run_delta(collect_files(args), args.root)
    for rel, line, cls, what in findings:
        print(
            f"::warning file={rel},line={line}::public-conformance[{cls}]: "
            f"matched policy pattern {what!r}"
        )
    print(
        f"public-conformance (delta, advisory): scanned {scanned} file(s); "
        f"{len(findings)} finding(s).",
        file=sys.stderr,
    )
    return 1 if (findings and args.enforce) else 0


if __name__ == "__main__":
    raise SystemExit(main())
