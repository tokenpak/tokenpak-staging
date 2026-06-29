"""Home-resolver guard for remaining legacy home callsites.

This is intentionally an allowlist, not a mass-rewrite test. Existing
``~/.tokenpak`` callsites are migrated incrementally; new ones should route
through ``tokenpak._paths`` instead.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / "tests" / "fixtures" / "std33_legacy_home_allowlist.txt"
LEGACY_PATTERN = re.compile(
    r'Path\.home\(\) / "\.tokenpak"'
    r'|os\.path\.expanduser\("~/\.tokenpak'
    r'|Path\("~/\.tokenpak'
)


def _legacy_matches() -> set[str]:
    matches: set[str] = set()
    for path in (ROOT / "tokenpak").rglob("*.py"):
        rel = path.relative_to(ROOT)
        if rel.parts[:2] == ("tokenpak", "tests"):
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if LEGACY_PATTERN.search(line):
                matches.add(f"{rel}:{line.rstrip()}")
    return matches


def test_no_new_legacy_tokenpak_home_callsites():
    allowed = {
        line.strip()
        for line in ALLOWLIST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    unexpected = sorted(_legacy_matches() - allowed)
    assert unexpected == []
