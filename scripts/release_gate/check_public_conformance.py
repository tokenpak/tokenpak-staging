#!/usr/bin/env python3
# ruff: noqa: E402,I001
"""Public-surface conformance gate.

Scans changed files or the tracked repository tree for public-surface policy
violations. Internal-reference detection is delegated to the shared structural
public-safety scanner; marketing and privacy phrase lists remain curated local
plaintext lists.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from public_safety_scan import (  # noqa: E402
    TEXT_SUFFIXES,
    collect_git_tracked,
    compiled_patterns,
    scan_text as scan_public_safety_text,
)

CONF_DIR = HERE / "conformance"

EXCLUDE_EXACT = frozenset(
    {
        ".github/workflows/identity-language-check.yml",
        ".github/workflows/public-conformance-check.yml",
        "scripts/release_gate/check_public_conformance.py",
    }
)
EXCLUDE_PREFIXES = (
    "scripts/release_gate/conformance/",
    "tests/",
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
    """Return (marketing_phrases, privacy_phrases, internal_pattern_specs)."""
    marketing = load_list("forbidden_marketing_phrases.txt")
    privacy = load_list("banned_privacy_claims.txt")
    internal = compiled_patterns("baseline")
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
    for finding in scan_public_safety_text(rel_path, text, internal):
        findings.append((rel_path, finding.line, "internal", finding.label))
    return findings


def scan_pyproject(rel_path, text, internal):
    """Flag public-safety findings inside author or maintainer tables."""
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
                for finding in scan_public_safety_text(rel_path, line, internal):
                    findings.append((rel_path, i, "pyproject", finding.label))
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


def _scan_paths(paths: list[str], root: Path):
    marketing, privacy, internal = load_patterns()
    findings = []
    scanned = 0
    for rel in paths:
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


def run_delta(files, root):
    return _scan_paths(files, Path(root))


def run_baseline(root):
    root = Path(root)
    files = [sf.relpath for sf in collect_git_tracked(str(root))]
    return _scan_paths(files, root)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="public-surface conformance gate")
    ap.add_argument("--mode", choices=["delta", "baseline"], default="delta")
    ap.add_argument("--changed-files-from", metavar="FILE")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--root", default=".")
    ap.add_argument("--enforce", action="store_true")
    args = ap.parse_args(argv)

    if args.mode == "baseline":
        scanned, findings = run_baseline(args.root)
    else:
        scanned, findings = run_delta(collect_files(args), args.root)

    annotation = "error" if args.enforce else "warning"
    for rel, line, cls, what in findings:
        print(
            f"::{annotation} file={rel},line={line}::public-conformance[{cls}]: "
            f"matched policy pattern {what!r}"
        )
    posture = "enforcing" if args.enforce else "recommended"
    print(
        f"public-conformance ({args.mode}, {posture}): scanned {scanned} file(s); "
        f"{len(findings)} finding(s).",
        file=sys.stderr,
    )
    return 1 if (findings and args.enforce) else 0


if __name__ == "__main__":
    raise SystemExit(main())
