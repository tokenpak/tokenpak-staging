# SPDX-License-Identifier: Apache-2.0
"""Std 33 path-resolution tests for PakLine (``statusline/pakline.sh``).

PakLine is a *pure reader* (PakLine architecture contract, 2026-06-20 §3.4):
it locates the per-session title-state file under the Std 33 canonical
TokenPak home (``~/.tpk/companion/titles``) following the documented
resolution order, and carries no hardcoded ``~/.tokenpak`` default. These
tests drive the shell script as a subprocess with an isolated ``HOME`` and
assert it reads the planted title from the resolved location.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PAKLINE = _REPO_ROOT / "tokenpak" / "companion" / "statusline" / "pakline.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="PakLine requires jq"
)


def _seed_title(companion_dir: Path, session_id: str, text: str) -> None:
    """Plant a title-state file at ``<companion_dir>/titles/<session_id>``."""
    d = companion_dir / "titles"
    d.mkdir(parents=True, exist_ok=True)
    (d / session_id).write_text(text + "\n")


def _run(payload: dict, *, home: Path, env_extra: dict | None = None):
    env = os.environ.copy()
    # Isolate HOME and drop any inherited overrides so the script resolves
    # purely from the test's HOME (and whatever env_extra sets explicitly).
    env["HOME"] = str(home)
    env["TOKENPAK_COMPANION_PAKLINE"] = "1"
    for k in ("TOKENPAK_HOME", "TOKENPAK_COMPANION_JOURNAL_DIR"):
        env.pop(k, None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(_PAKLINE)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )


# --------------------------------------------------------------------------- #
# Resolution order: TOKENPAK_HOME → ~/.tpk → ~/.tokenpak (legacy) → ~/.tpk
# --------------------------------------------------------------------------- #


def test_resolves_under_canonical_tpk_home(tmp_path):
    """Canonical ``~/.tpk/companion`` is used when it exists."""
    _seed_title(tmp_path / ".tpk" / "companion", "s1", "Canon task")
    r = _run({"session_id": "s1", "cost": {"total_cost_usd": 0}}, home=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("\U0001F4E6 Canon task")


def test_legacy_tokenpak_only_when_canonical_absent(tmp_path):
    """Legacy ``~/.tokenpak/companion`` is honored only when ``~/.tpk`` is absent."""
    _seed_title(tmp_path / ".tokenpak" / "companion", "s1", "Legacy task")
    r = _run({"session_id": "s1", "cost": {"total_cost_usd": 0}}, home=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("\U0001F4E6 Legacy task")


def test_canonical_wins_over_legacy(tmp_path):
    """When both homes exist, canonical ``~/.tpk`` wins."""
    _seed_title(tmp_path / ".tpk" / "companion", "s1", "Canon")
    _seed_title(tmp_path / ".tokenpak" / "companion", "s1", "Legacy")
    r = _run({"session_id": "s1", "cost": {"total_cost_usd": 0}}, home=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("\U0001F4E6 Canon")


def test_tokenpak_home_env_override_wins(tmp_path):
    """``TOKENPAK_HOME`` takes precedence over ``~/.tpk``."""
    sandbox = tmp_path / "sandbox"
    _seed_title(sandbox / "companion", "s1", "Sandbox task")
    _seed_title(tmp_path / ".tpk" / "companion", "s1", "Should not win")
    r = _run(
        {"session_id": "s1", "cost": {"total_cost_usd": 0}},
        home=tmp_path,
        env_extra={"TOKENPAK_HOME": str(sandbox)},
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("\U0001F4E6 Sandbox task")


def test_companion_journal_dir_override_wins(tmp_path):
    """The companion's own ``TOKENPAK_COMPANION_JOURNAL_DIR`` override wins outright."""
    jdir = tmp_path / "explicit"
    _seed_title(jdir, "s1", "Explicit journal")
    _seed_title(tmp_path / ".tpk" / "companion", "s1", "Should not win")
    r = _run(
        {"session_id": "s1", "cost": {"total_cost_usd": 0}},
        home=tmp_path,
        env_extra={"TOKENPAK_COMPANION_JOURNAL_DIR": str(jdir)},
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("\U0001F4E6 Explicit journal")


# --------------------------------------------------------------------------- #
# Source-level regression locks
# --------------------------------------------------------------------------- #


def test_no_hardcoded_legacy_default_in_source():
    """The migration removed the hardcoded ``~/.tokenpak/companion`` default."""
    src = _PAKLINE.read_text()
    assert ":-$HOME/.tokenpak/companion}" not in src
    assert ".tpk" in src  # Std 33 canonical home is referenced


def test_no_dangling_provenance_spec_reference():
    """The dangling ``provenance-spec.md`` file reference was resolved."""
    src = _PAKLINE.read_text()
    assert "provenance-spec.md" not in src
