# SPDX-License-Identifier: Apache-2.0
"""Regression tests for the trust-conformance release gate.

These lock in the specific public-artifact drift shapes that previously
reshipped undetected — a missing NOTICE, a retired compression-era hero, a
"same results" equivalence claim, an unqualified savings percentage, and an
untiered "works with <products>" client list — so none of them can pass the
gate again. A conformant tree must still pass cleanly, and advisory mode must
never fail the build regardless of drift.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE_PATH = _REPO_ROOT / "scripts" / "release_gate" / "trust_conformance.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("trust_conformance", _GATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass annotation resolution can find the module.
    sys.modules["trust_conformance"] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()


CONFORMANT_FILES = {
    "NOTICE": (
        "TokenPak\nCopyright 2026 TokenPak\n\n"
        "This product is licensed under the Apache License, Version 2.0.\n"
    ),
    "LICENSE": "Apache License\nVersion 2.0, January 2004\n\n(full text)\n",
    "SECURITY.md": (
        "# Security Policy\n\n## Reporting a Security Issue\n\n"
        "Email: hello@tokenpak.ai\n"
    ),
    "CONTRIBUTING.md": (
        "# Contributing\n\n## Developer Certificate of Origin (DCO)\n\n"
        "Commit with `-s` to add the Signed-off-by trailer.\n"
    ),
    "README.md": (
        "# TokenPak — Cut your LLM token spend — zero config\n\n"
        "> **The open logistics layer for AI context.**\n\n"
        "TokenPak packs AI requests before they ship.\n"
    ),
    "pyproject.toml": (
        '[project]\nname = "tokenpak"\n'
        'classifiers = ["License :: OSI Approved :: Apache Software License"]\n'
        '[project.license]\nfile = "LICENSE"\n'
    ),
}


def _write_tree(root: Path, overrides=None, drop=()):
    files = dict(CONFORMANT_FILES)
    for name in drop:
        files.pop(name, None)
    if overrides:
        files.update(overrides)
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")


def _errors(root: Path):
    return gate.run(root, scope="staging", mode="enforcing").errors


def test_conformant_tree_passes(tmp_path):
    _write_tree(tmp_path)
    assert _errors(tmp_path) == []


def test_missing_notice_fails(tmp_path):
    _write_tree(tmp_path, drop=("NOTICE",))
    assert any(e.check == "notice" for e in _errors(tmp_path))


def test_missing_license_fails(tmp_path):
    _write_tree(tmp_path, drop=("LICENSE",))
    assert any(e.check == "license" for e in _errors(tmp_path))


def test_missing_security_fails(tmp_path):
    _write_tree(tmp_path, drop=("SECURITY.md",))
    assert any(e.check == "security" for e in _errors(tmp_path))


def test_missing_dco_fails(tmp_path):
    _write_tree(tmp_path, overrides={"CONTRIBUTING.md": "# Contributing\n\nOpen a PR.\n"})
    assert any(e.check == "contributing" for e in _errors(tmp_path))


def test_missing_canonical_hero_fails(tmp_path):
    _write_tree(tmp_path, overrides={
        "README.md": "# TokenPak\n\nA tool.\n"})
    assert any(e.check == "hero" for e in _errors(tmp_path))


def test_retired_compression_hero_fails(tmp_path):
    _write_tree(tmp_path, overrides={
        "README.md": CONFORMANT_FILES["README.md"]
        + "\nTokenPak compresses your LLM context deterministically.\n"})
    assert any(e.check == "claim-regression" for e in _errors(tmp_path))


def test_same_results_claim_fails(tmp_path):
    _write_tree(tmp_path, overrides={
        "README.md": CONFORMANT_FILES["README.md"] + "\nSame results, fewer tokens.\n"})
    assert any(e.check == "claim-regression" for e in _errors(tmp_path))


def test_unqualified_savings_pct_fails(tmp_path):
    _write_tree(tmp_path, overrides={
        "README.md": CONFORMANT_FILES["README.md"] + "\nSave 40% on every request.\n"})
    assert any(e.check == "claim-regression" for e in _errors(tmp_path))


def test_untiered_client_list_fails(tmp_path):
    _write_tree(tmp_path, overrides={
        "README.md": CONFORMANT_FILES["README.md"]
        + "\nWorks with Claude Code, Cursor, Cline, Aider\n"})
    assert any(e.check == "claim-regression" for e in _errors(tmp_path))


def test_tiered_client_list_passes(tmp_path):
    # A qualified ("Pro") client list is allowed and must not red-bar.
    _write_tree(tmp_path, overrides={
        "README.md": CONFORMANT_FILES["README.md"]
        + "\nWorks with Claude Code, Cursor, and other Pro-tier integrations.\n"})
    assert not any(e.check == "claim-regression" for e in _errors(tmp_path))


def test_advisory_mode_never_fails(tmp_path):
    _write_tree(tmp_path, drop=("NOTICE", "SECURITY.md"))
    assert gate.main(["--root", str(tmp_path), "--mode", "advisory"]) == 0


def test_enforcing_mode_nonzero_on_drift(tmp_path):
    _write_tree(tmp_path, drop=("NOTICE",))
    assert gate.main(["--root", str(tmp_path), "--mode", "enforcing"]) == 1
