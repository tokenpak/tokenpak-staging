#!/usr/bin/env python3
"""check_version_consistency.py — release preflight: code version == built metadata.

Asserts that the in-code version (``tokenpak.__version__`` in
``tokenpak/__init__.py`` — the single source of truth that ``pyproject.toml``
consumes via ``[tool.setuptools.dynamic]``) matches the version recorded in the
built / installed distribution metadata (``importlib.metadata.version``).

A mismatch means a release would ship a wheel whose metadata version disagrees
with the code — for example a stale editable install left on a build host. This
gate fails the release path before such a skew can be published. It never bumps
or edits a version; it only compares and reports.

Usage:
    python3 scripts/release_gate/check_version_consistency.py

Exit codes:
    0 — code version and built/dist metadata agree
    1 — version skew detected (release must not proceed)
    2 — a version could not be determined (source or build/inspect failure)
"""

from __future__ import annotations

import re
import sys
from importlib import metadata as importlib_metadata
from pathlib import Path

DIST_NAME = "tokenpak"
REPO_ROOT = Path(__file__).resolve().parents[2]
_VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def read_code_version(root: Path = REPO_ROOT) -> str:
    """Return ``__version__`` parsed directly from ``tokenpak/__init__.py``.

    Parses the source instead of importing the package, so the check works in a
    bare source tree and always reflects the *code* version, independent of what
    an installed or editable distribution advertises.
    """
    init_path = root / "tokenpak" / "__init__.py"
    text = init_path.read_text(encoding="utf-8")
    match = _VERSION_RE.search(text)
    if match is None:
        raise ValueError(f"no __version__ assignment found in {init_path}")
    return match.group(1)


def read_built_version(dist_name: str = DIST_NAME) -> str:
    """Return the version recorded in the installed distribution metadata."""
    return importlib_metadata.version(dist_name)


def compare_versions(code_version: str, built_version: str) -> int:
    """Return 0 when the versions agree, 1 when they skew (printing a report)."""
    if code_version == built_version:
        print(f"OK: code version == built metadata ({code_version})")
        return 0
    print(
        f"VERSION SKEW: code version={code_version!r} != "
        f"built metadata={built_version!r}",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    try:
        code_version = read_code_version()
        built_version = read_built_version()
    except FileNotFoundError as exc:
        print(f"ERROR: source not found: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except importlib_metadata.PackageNotFoundError:
        print(
            f"ERROR: distribution {DIST_NAME!r} is not installed; "
            "cannot read built metadata",
            file=sys.stderr,
        )
        return 2
    return compare_versions(code_version, built_version)


if __name__ == "__main__":
    raise SystemExit(main())
