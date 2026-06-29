# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

from tokenpak import _cli_core

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _flat(path: str) -> str:
    return " ".join(_read(path).split())


def test_readme_surfaces_pro_boundary_and_trust_docs():
    text = _read("README.md")

    assert "tokenpak upgrade --print-url" in text
    assert "docs/multipak.md" in text
    assert "docs/guides/enterprise/security-architecture.md" in text
    assert "docs/guides/enterprise/compliance-mapping.md" in text
    assert "do not change the beta support model" in text


def test_quickstart_surfaces_trust_paths_without_overclaiming():
    text = _read("docs/quickstart.md")
    flat = _flat("docs/quickstart.md")

    assert "tokenpak upgrade --print-url" in text
    assert "./multipak.md" in text
    assert "./guides/enterprise/security-architecture.md" in text
    assert "./guides/enterprise/compliance-mapping.md" in text
    assert "do not change the beta support model" in flat
    assert "hosted processing by the OSS package" in flat


def test_docs_index_has_trust_and_editions_section():
    text = _read("docs/README.md")

    assert "## Trust & Editions" in text
    assert "MultiPak / Pro boundary" in text
    assert "guides/enterprise/security-architecture.md" in text
    assert "guides/enterprise/compliance-mapping.md" in text
    assert "KNOWN_LIMITATIONS.md" in text


def test_compliance_stub_points_to_docs(capsys):
    parser = _cli_core.build_parser()
    args = parser.parse_args(["compliance"])

    assert args.func(args) == 1
    out = capsys.readouterr().out
    assert "Pro/Enterprise" in out
    assert "docs/guides/enterprise/compliance-mapping.md" in out


def test_audit_stub_points_to_security_docs(capsys):
    parser = _cli_core.build_parser()
    args = parser.parse_args(["audit"])

    assert args.func(args) == 1
    out = capsys.readouterr().out
    assert "Pro/Enterprise" in out
    assert "docs/guides/enterprise/security-architecture.md" in out
