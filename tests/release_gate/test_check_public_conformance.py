"""Unit tests for the public-conformance delta scan.

Loaded by path so the test does not depend on a particular package layout for
``scripts/``. Lives under tests/ (which both the conformance scan and the
identity scan skip), so the deliberate trigger fixtures below — task-ID and
private-path forms from the leak register — do not flag the test file itself.
"""
import importlib.util
from pathlib import Path

_MOD_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "release_gate"
    / "check_public_conformance.py"
)
_spec = importlib.util.spec_from_file_location("check_public_conformance", _MOD_PATH)
cpc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cpc)


def _scan(text, name="docs/example.md"):
    marketing, privacy, internal = cpc.load_patterns()
    return cpc.scan_text(name, text, marketing, privacy, internal)


def _classes(findings):
    return {cls for _, _, cls, _ in findings}


def test_marketing_phrase_flagged():
    assert "marketing" in _classes(_scan("Our revolutionary, industry-leading proxy."))


def test_savings_percentage_flagged():
    assert "marketing" in _classes(_scan("Customers see 90% savings on day one."))


def test_privacy_overclaim_flagged():
    assert "privacy" in _classes(_scan("TokenPak is privacy-first and fully private."))


def test_internal_leak_flagged():
    # neutral leak-register forms (task-ID + private path), no agent name
    findings = _scan("Tracked in TSR-42; logs under /home/sue/runs.")
    assert "internal" in _classes(findings)


def test_clean_public_copy_has_no_findings():
    clean = "TokenPak routes each request to your provider and reuses cached responses."
    assert _scan(clean) == []


def test_excluded_paths_are_skipped():
    assert cpc.is_excluded("scripts/release_gate/conformance/forbidden_marketing_phrases.txt")
    assert cpc.is_excluded("tests/release_gate/test_check_public_conformance.py")
    assert cpc.is_excluded("scripts/release_gate/check_public_conformance.py")
    assert not cpc.is_excluded("README.md")


def test_advisory_mode_exits_zero_even_with_findings(tmp_path):
    bad = tmp_path / "docs"
    bad.mkdir()
    (bad / "page.md").write_text("revolutionary privacy-first tool", encoding="utf-8")
    changed = tmp_path / "changed.txt"
    changed.write_text("docs/page.md\n", encoding="utf-8")
    rc = cpc.main(
        ["--mode", "delta", "--changed-files-from", str(changed), "--root", str(tmp_path)]
    )
    assert rc == 0  # advisory: findings do not fail


def test_enforce_mode_exits_nonzero_with_findings(tmp_path):
    bad = tmp_path / "docs"
    bad.mkdir()
    (bad / "page.md").write_text("game-changing", encoding="utf-8")
    changed = tmp_path / "changed.txt"
    changed.write_text("docs/page.md\n", encoding="utf-8")
    rc = cpc.main(
        [
            "--mode",
            "delta",
            "--changed-files-from",
            str(changed),
            "--root",
            str(tmp_path),
            "--enforce",
        ]
    )
    assert rc == 1
