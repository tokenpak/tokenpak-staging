# SPDX-License-Identifier: Apache-2.0
"""CLI-level regression tests for the trust-conformance release gate."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

_CONFORMANT_FILES = {
    "NOTICE": (
        "TokenPak\n"
        "Copyright 2026 TokenPak\n\n"
        "This product is licensed under the Apache License, Version 2.0.\n"
    ),
    "LICENSE": "Apache License\nVersion 2.0, January 2004\n\n",
    "SECURITY.md": (
        "# Security Policy\n\n"
        "## Reporting a Security Issue\n\n"
        "Email security reports to security@tokenpak.ai.\n"
    ),
    "CONTRIBUTING.md": (
        "# Contributing\n\n"
        "## Developer Certificate of Origin\n\n"
        "Use a Signed-off-by trailer on commits.\n"
    ),
    "README.md": (
        "# TokenPak\n\n"
        "The open logistics layer for AI context.\n"
    ),
    "pyproject.toml": (
        '[project]\n'
        'name = "tokenpak"\n'
        'license = "Apache-2.0"\n'
        'classifiers = ["License :: OSI Approved :: Apache Software License"]\n'
    ),
}


def _write_tree(root: Path, *, overrides: dict[str, str] | None = None,
                drop: tuple[str, ...] = ()) -> None:
    files = dict(_CONFORMANT_FILES)
    for name in drop:
        files.pop(name, None)
    if overrides:
        files.update(overrides)
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")


def _run_gate(root: Path) -> tuple[subprocess.CompletedProcess[str], dict]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_REPO_ROOT)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.release_gate.trust_conformance",
            "--root",
            str(root),
            "--scope",
            "staging",
            "--mode",
            "enforcing",
            "--json",
        ],
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    payload = json.loads(result.stdout)
    return result, payload


def test_enforcing_mode_accepts_conformant_tree(tmp_path):
    _write_tree(tmp_path)

    result, payload = _run_gate(tmp_path)

    assert result.returncode == 0, result.stderr
    assert payload["errors"] == 0


def test_enforcing_mode_rejects_missing_notice(tmp_path):
    _write_tree(tmp_path, drop=("NOTICE",))

    result, payload = _run_gate(tmp_path)

    assert result.returncode == 1
    assert any(
        finding["severity"] == "ERROR" and finding["check"] == "notice"
        for finding in payload["findings"]
    )


def test_enforcing_mode_rejects_retired_public_claim(tmp_path):
    _write_tree(
        tmp_path,
        overrides={
            "README.md": (
                _CONFORMANT_FILES["README.md"]
                + "\nSame results, fewer tokens.\n"
            ),
        },
    )

    result, payload = _run_gate(tmp_path)

    assert result.returncode == 1
    assert any(
        finding["severity"] == "ERROR"
        and finding["check"] == "claim-regression"
        for finding in payload["findings"]
    )
