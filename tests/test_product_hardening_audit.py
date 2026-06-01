# SPDX-License-Identifier: Apache-2.0
"""Tests for the product-hardening audit / release-readiness bundle.

Covers proposal 2026-04-29 §S1.2 (`make audit`), §S2.2/§S2.3 (integration
detector), §S3.1 (CLAIMS.md), §S3.2 (`make release-readiness`).

All tests in this file are deterministic, local-only, and require no live
provider calls.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass can resolve cls.__module__ during decoration.
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# ---------------------------------------------------------------------------
# Internal-leakage detector
# ---------------------------------------------------------------------------


@pytest.mark.quick
def test_audit_internal_leakage_module_loads():
    mod = _load_module(
        "tokenpak_audit_leakage", SCRIPTS / "audit_internal_leakage.py"
    )
    assert mod.LEAK_PATTERNS, "leak patterns registry should be non-empty"
    # Each pattern is (regex_str, kind_label).
    for pat, kind in mod.LEAK_PATTERNS:
        assert isinstance(pat, str)
        assert isinstance(kind, str)
        assert kind


@pytest.mark.quick
def test_audit_internal_leakage_clean_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    # Plant inside the shipped surface (tokenpak/) so the scanner actually
    # inspects this file under its default include-filter.
    pkg = repo / "tokenpak"
    pkg.mkdir()
    (pkg / "ok.py").write_text("print('public-safe content')\n")
    subprocess.run(["git", "add", "tokenpak/ok.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    rc = subprocess.run(
        [sys.executable, str(SCRIPTS / "audit_internal_leakage.py"),
         "--root", str(repo), "--json"],
        capture_output=True, text=True,
    )
    assert rc.returncode == 0, rc.stdout + rc.stderr
    payload = json.loads(rc.stdout)
    assert payload["ok"] is True
    assert payload["findings"] == []


@pytest.mark.quick
def test_audit_internal_leakage_detects_planted_leak(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    # Plant a leak pattern under the shipped surface (tokenpak/) so the
    # scanner's default include-filter actually picks it up.
    pkg = repo / "tokenpak"
    pkg.mkdir()
    (pkg / "leak.py").write_text(
        '"""Notes\n\nSee /vault/03_AGENT_PACKS/Trix/queue/ for context."""\n'
    )
    subprocess.run(["git", "add", "tokenpak/leak.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "leak"], cwd=repo, check=True)

    rc = subprocess.run(
        [sys.executable, str(SCRIPTS / "audit_internal_leakage.py"),
         "--root", str(repo), "--json"],
        capture_output=True, text=True,
    )
    assert rc.returncode == 1
    payload = json.loads(rc.stdout)
    assert payload["ok"] is False
    kinds = {f["kind"] for f in payload["findings"]}
    # Internal vault path AND vault tree ref both fire on this line.
    assert "internal-vault-path" in kinds or "vault-tree-ref" in kinds


# ---------------------------------------------------------------------------
# Package dry-run validation
# ---------------------------------------------------------------------------


@pytest.mark.quick
def test_audit_package_dryrun_passes_on_workbench():
    rc = subprocess.run(
        [sys.executable, str(SCRIPTS / "audit_package_dryrun.py"),
         "--root", str(REPO_ROOT), "--json"],
        capture_output=True, text=True,
    )
    assert rc.returncode == 0, rc.stdout + rc.stderr
    payload = json.loads(rc.stdout)
    assert payload["ok"] is True
    assert any("tokenpak.__version__" in note for note in payload["notes"])


@pytest.mark.quick
def test_audit_package_dryrun_flags_missing_top_level(tmp_path):
    # Empty git repo: README.md, LICENSE, pyproject.toml all absent.
    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    rc = subprocess.run(
        [sys.executable, str(SCRIPTS / "audit_package_dryrun.py"),
         "--root", str(repo), "--json"],
        capture_output=True, text=True,
    )
    assert rc.returncode == 1
    payload = json.loads(rc.stdout)
    missing = {f.get("missing") for f in payload["failures"] if f.get("check") == "required-top-level"}
    assert "README.md" in missing
    assert "LICENSE" in missing
    assert "pyproject.toml" in missing


# ---------------------------------------------------------------------------
# Integration detector — three personas
# ---------------------------------------------------------------------------


@pytest.fixture
def detector():
    return _load_module(
        "tokenpak_integration_detector", SCRIPTS / "integration_detector.py"
    )


@pytest.mark.quick
def test_detector_anthropic_persona(detector, monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-redacted")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    found = detector.detect_all(home=tmp_path)
    clients = [d.client for d in found]
    assert "anthropic-sdk" in clients
    anthropic = next(d for d in found if d.client == "anthropic-sdk")
    assert anthropic.confidence == "high"
    assert any("ANTHROPIC_BASE_URL" in m for m in anthropic.missing_steps)
    assert any("tokenpak serve" in c for c in anthropic.next_commands)


@pytest.mark.quick
def test_detector_anthropic_already_proxied(detector, monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8766")
    found = detector.detect_all(home=tmp_path)
    anthropic = next(d for d in found if d.client == "anthropic-sdk")
    assert anthropic.missing_steps == []
    assert any("status" in c for c in anthropic.next_commands)


@pytest.mark.quick
def test_detector_openai_codex_persona(detector, monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    found = detector.detect_all(home=tmp_path)
    clients = [d.client for d in found]
    assert "openai-sdk-or-codex" in clients
    openai = next(d for d in found if d.client == "openai-sdk-or-codex")
    assert openai.confidence == "high"
    assert any("OPENAI_BASE_URL" in m for m in openai.missing_steps)


@pytest.mark.quick
def test_detector_claude_code_persona(detector, monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text("{}")
    found = detector.detect_all(home=tmp_path)
    clients = [d.client for d in found]
    assert "claude-code" in clients
    cc = next(d for d in found if d.client == "claude-code")
    assert any("integrate claude-code" in c for c in cc.next_commands)


@pytest.mark.quick
def test_detector_no_signals(detector, monkeypatch, tmp_path):
    for k in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_OAUTH_TOKEN", "OPENAI_CODEX_OAUTH",
    ):
        monkeypatch.delenv(k, raising=False)
    # Empty home directory: no ~/.claude, no ~/.cursor, no ~/.tokenpak.
    found = detector.detect_all(home=tmp_path)
    # `aider` may or may not be on PATH; allow either 0 or 1 detections from
    # that one binary check.
    aider_only = [d for d in found if d.client != "aider"]
    assert aider_only == [], f"unexpected detections: {aider_only}"


@pytest.mark.quick
def test_detector_json_mode_cli(monkeypatch, tmp_path):
    """End-to-end: invoke the script as a subprocess, parse --json output."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                        "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL",
                        "CLAUDE_CODE_OAUTH_TOKEN")}
    env["ANTHROPIC_API_KEY"] = "sk-test"
    rc = subprocess.run(
        [sys.executable, str(SCRIPTS / "integration_detector.py"),
         "--json", "--home", str(tmp_path)],
        capture_output=True, text=True, env=env,
    )
    assert rc.returncode == 0, rc.stderr
    payload = json.loads(rc.stdout)
    assert payload["detected_count"] >= 1
    clients = [d["client"] for d in payload["detections"]]
    assert "anthropic-sdk" in clients


# ---------------------------------------------------------------------------
# CLAIMS.md presence
# ---------------------------------------------------------------------------


@pytest.mark.quick
def test_claims_md_present_and_substantial():
    claims = REPO_ROOT / "CLAIMS.md"
    assert claims.exists(), "CLAIMS.md must exist at workbench root (S3.1)"
    body = claims.read_text()
    # Each headline claim category must appear as a table row.
    must_mention = [
        "30–50%",
        "Local-first",
        "No credentials stored",
        "Benchmark reproduction",
    ]
    for token in must_mention:
        assert token in body, f"CLAIMS.md missing claim row for: {token!r}"
    # Must have a table header with the canonical six columns.
    header = "| Claim | Evidence command | CI / workflow coverage | Last verified | Caveats | Owner |"
    assert header in body, "CLAIMS.md must have the six-column claims table header"


# ---------------------------------------------------------------------------
# Release-readiness report
# ---------------------------------------------------------------------------


@pytest.fixture
def release_readiness():
    return _load_module(
        "tokenpak_release_readiness", SCRIPTS / "release_readiness.py"
    )


@pytest.mark.quick
def test_release_readiness_renders_required_sections():
    """The advisory report must include every required §S3.2 section."""
    rc = subprocess.run(
        [sys.executable, str(SCRIPTS / "release_readiness.py"),
         "--root", str(REPO_ROOT), "--json",
         "--skip-audit", "--skip-tests"],
        capture_output=True, text=True, timeout=60,
    )
    assert rc.returncode == 0, rc.stderr
    payload = json.loads(rc.stdout)
    assert payload["advisory"] is True
    names = [s["name"] for s in payload["sections"]]
    required = {
        "version", "git", "tests-quick", "audit",
        "package-dryrun", "internal-leakage", "claims",
        "docs-inventory", "known-blockers",
    }
    assert required.issubset(set(names)), \
        f"missing required sections: {required - set(names)}"
    assert payload["recommendation"] in {"go", "go-with-caveats", "no-go"}


@pytest.mark.quick
def test_release_readiness_recommendation_logic(release_readiness):
    """Section status drives the recommendation; verify the mapping."""
    S = release_readiness.Section
    all_ok = [S("a", "ok"), S("b", "ok")]
    with_warn = [S("a", "ok"), S("b", "warn")]
    with_fail = [S("a", "ok"), S("b", "fail")]
    rec, _ = release_readiness.recommendation(all_ok)
    assert rec == "go"
    rec, _ = release_readiness.recommendation(with_warn)
    assert rec == "go-with-caveats"
    rec, reasons = release_readiness.recommendation(with_fail)
    assert rec == "no-go"
    assert any(r.startswith("fail:") for r in reasons)


@pytest.mark.quick
def test_release_readiness_strict_exits_nonzero_on_no_go(tmp_path):
    """--strict + no-go must produce a non-zero exit (for CI gating)."""
    # Use a stripped-down repo with no tokenpak/__init__.py → version section fails.
    empty = tmp_path / "empty"
    empty.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=empty, check=True)
    rc = subprocess.run(
        [sys.executable, str(SCRIPTS / "release_readiness.py"),
         "--root", str(empty), "--json",
         "--skip-audit", "--skip-tests", "--strict"],
        capture_output=True, text=True, timeout=60,
    )
    assert rc.returncode == 1


# ---------------------------------------------------------------------------
# audit.sh smoke
# ---------------------------------------------------------------------------


@pytest.mark.quick
def test_audit_sh_skip_all_succeeds(tmp_path):
    """audit.sh with every check skipped should report PASS (no checks ran).

    This pins the contract that the SKIP_<NAME>=1 envs work and that an
    empty-but-error-free run is reported as PASS, not silently broken.
    """
    env = os.environ.copy()
    env.update({
        "SKIP_QUICK": "1",
        "SKIP_CLI_HELP": "1",
        "SKIP_CLI_DOCS": "1",
        "SKIP_DOCS_INVENTORY": "1",
        "SKIP_PACKAGE_DRYRUN": "1",
        "SKIP_LEAK": "1",
    })
    rc = subprocess.run(
        ["bash", str(SCRIPTS / "audit.sh")],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert rc.returncode == 0, rc.stdout + rc.stderr
    assert "AUDIT PASS" in rc.stdout
