"""Regression guard: fabricated savings values must never re-enter the trust surface.

The savings/cost CLI surface must only display receipt-backed numbers produced by
the honest savings engine (``get_savings_report``), or a neutral
"data unavailable" state. It must never invent figures via magic multipliers,
model-name string-matching, or corrupt/unreliable columns.

This test scans the shipped trust-surface source files for the specific
fabrication patterns that were removed and fails if any of them reappear.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Repo root = parent of the tests/ directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Trust-surface files that render savings/cost numbers to users. These must stay
# free of fabricated values.
TRUST_SURFACE_FILES = [
    _REPO_ROOT / "tokenpak" / "_cli_core.py",
    _REPO_ROOT / "tokenpak" / "cli" / "commands" / "status.py",
]

# (label, compiled regex) for each forbidden fabrication pattern.
# Each pattern matches a magic multiplier / fabricated figure that was removed.
FORBIDDEN_PATTERNS = [
    # Invented "top model saved ~95%" attribution multiplier.
    ("savings_amount * 0.95 attribution multiplier", re.compile(r"\*\s*0\.95")),
    # Invented "cache is 30% of input" split.
    ("cache_read = input * 0.30 invented split", re.compile(r"\*\s*0\.30")),
    # Invented "assume 35% saved" leaderboard estimate.
    ("estimated_saved = cost * 0.35 invented estimate", re.compile(r"\*\s*0\.35")),
    # Model-name astrology: percentages assigned by string-matching the model name.
    (
        "model-name-astrology cache_pct/compress_pct",
        re.compile(r"(cache_pct|compress_pct)\s*=\s*\d.*if\s+[\"']\w+[\"']\s+in\s+model"),
    ),
]


def _scan(text: str, pattern: re.Pattern) -> list[tuple[int, str]]:
    hits = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if pattern.search(line):
            hits.append((lineno, line.strip()))
    return hits


@pytest.mark.parametrize("path", TRUST_SURFACE_FILES, ids=lambda p: p.name)
def test_trust_surface_has_no_fabricated_savings(path: Path) -> None:
    """No fabricated savings/cost figure may appear in a trust-surface file."""
    assert path.exists(), f"trust-surface file missing: {path}"
    text = path.read_text(encoding="utf-8")

    violations: list[str] = []
    for label, pattern in FORBIDDEN_PATTERNS:
        for lineno, line in _scan(text, pattern):
            violations.append(f"{path.name}:{lineno}: [{label}] {line}")

    assert not violations, (
        "Fabricated savings value(s) re-entered the trust surface:\n"
        + "\n".join(violations)
    )


def test_savings_command_routes_through_honest_engine() -> None:
    """``tokenpak savings`` must route through the canonical conservative
    compute_savings engine, never the corrupt monitor.db ``compressed_tokens`` path."""
    core = (_REPO_ROOT / "tokenpak" / "_cli_core.py").read_text(encoding="utf-8")

    # Isolate the cmd_savings function body.
    start = core.index("def cmd_savings(")
    # Next top-level def after cmd_savings marks the end of its body.
    end = core.index("\ndef ", start + 1)
    body = core[start:end]

    assert "compute_savings" in body, (
        "cmd_savings must route through the canonical conservative compute_savings engine"
    )
    assert "_monitor_db_savings" not in body, (
        "cmd_savings must not read the corrupt monitor.db compressed_tokens path"
    )
    assert "compressed_tokens" not in body, (
        "cmd_savings must not read the corrupt compressed_tokens column"
    )
