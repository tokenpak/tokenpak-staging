"""Static fail-closed wiring checks for the release-check Make targets."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"


def _target_prerequisites(name: str) -> set[str]:
    text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}:\s+([^#\n]+)", text, re.MULTILINE)
    assert match, f"target {name!r} is missing or has no prerequisites"
    return set(match.group(1).split())


def _target_block(name: str) -> str:
    text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(
        rf"^{re.escape(name)}:.*?(?=^[A-Za-z0-9_.-]+:|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match, f"target {name!r} is missing"
    return match.group(0)


def test_audit_maps_every_std09_automated_component():
    assert _target_prerequisites("audit") == {
        "ci-lint",
        "audit-mypy",
        "docs-check",
        "forbidden-phrases-check",
        "telemetry-audit",
    }


def test_release_check_maps_every_named_std10_gate():
    assert _target_prerequisites("release-check") == {
        "release-check-baseline",
        "test",
        "test-quick",
        "lint-imports",
        "fresh-install-demo",
        "bench",
        "byte-fidelity-check",
        "audit",
        "release-docs-pattern-check",
    }


def test_release_check_does_not_activate_deferred_formatter_ratchet():
    assert "check" not in _target_prerequisites("release-check")
    assert "format-check" not in _target_prerequisites("release-check")


def test_composites_do_not_mask_prerequisite_failure():
    for target in ("audit", "release-check"):
        block = _target_block(target)
        assert "|| true" not in block
        assert "continue-on-error" not in block
        assert not any(line.startswith("\t-") for line in block.splitlines())
