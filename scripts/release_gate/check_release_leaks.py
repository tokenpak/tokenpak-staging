#!/usr/bin/env python3
"""Full-tree public-leak release gate.

This wrapper preserves the historical release-gate entrypoint while delegating
to the shared public-side scanner. The scanner uses structural public-safety
classes only; exact private identifiers remain in the internal register outside
public CI.
"""

from __future__ import annotations

import argparse
import tempfile

from public_safety_scan import collect_dist, collect_tree, report_findings, scan_files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--dist",
        metavar="DIR",
        help="directory containing built sdist (*.tar.gz) and/or wheel (*.whl)",
    )
    src.add_argument(
        "--tree",
        metavar="DIR",
        help="directory tree to scan (repo-relative paths computed from DIR)",
    )
    args = ap.parse_args(argv)

    with tempfile.TemporaryDirectory() as workdir:
        if args.dist:
            files = collect_dist(args.dist, workdir)
            source_desc = f"distribution artifacts in {args.dist}"
        else:
            files = collect_tree(args.tree)
            source_desc = f"tree {args.tree}"

        findings = scan_files(files)

    if findings:
        report_findings(findings, source_desc, "error")
        print(
            "These findings ship to users. If a match is a legitimate public "
            "surface, add a narrow structural mask in the shared scanner."
        )
        return 1

    print(f"OK: scanned {len(files)} shipped file(s) in {source_desc}; no leaks found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
