# SPDX-License-Identifier: Apache-2.0
"""Tests for the companion visible-surface guard."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_MOD_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "release_gate"
    / "check_companion_visible_surfaces.py"
)
_spec = importlib.util.spec_from_file_location("check_companion_visible_surfaces", _MOD_PATH)
cvs = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = cvs
_spec.loader.exec_module(cvs)


def _write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _seed_required(root: Path) -> None:
    _write(
        root,
        "tokenpak/companion/hooks/pre_send.sh",
        "hookSpecificOutput sessionTitle titles\n",
    )
    _write(root, "tokenpak/companion/hooks/session_start_name.sh")
    _write(
        root,
        "tokenpak/companion/statusline/pakline.sh",
        "total_cost_usd exceeds_200k_tokens total_duration_ms\n",
    )
    _write(
        root,
        "tokenpak/companion/codex/statusline_config.py",
        "thread-title context-remaining context-used context-window-size task-progress\n",
    )
    _write(root, "tokenpak/companion/codex/state_lock.py")


def test_guard_passes_minimal_seeded_surface(tmp_path):
    _seed_required(tmp_path)
    assert cvs.run(tmp_path) == []


def test_guard_fails_missing_required_surface(tmp_path):
    _seed_required(tmp_path)
    (tmp_path / "tokenpak/companion/statusline/pakline.sh").unlink()
    findings = cvs.run(tmp_path)
    assert any("required companion visible-surface source" in f.message for f in findings)


def test_guard_fails_pyc_without_matching_source(tmp_path):
    _seed_required(tmp_path)
    pyc = tmp_path / "tokenpak/companion/codex/__pycache__/missing.cpython-312.pyc"
    pyc.parent.mkdir(parents=True, exist_ok=True)
    pyc.write_bytes(b"0")

    findings = cvs.run(tmp_path)
    assert any("bytecode has no matching source" in f.message for f in findings)


def test_guard_accepts_pyc_with_matching_source(tmp_path):
    _seed_required(tmp_path)
    _write(tmp_path, "tokenpak/companion/codex/present.py")
    pyc = tmp_path / "tokenpak/companion/codex/__pycache__/present.cpython-312.pyc"
    pyc.parent.mkdir(parents=True, exist_ok=True)
    pyc.write_bytes(b"0")

    assert cvs.run(tmp_path) == []


def test_guard_fails_hooks_json_missing_script(tmp_path):
    _seed_required(tmp_path)
    hooks = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"command": "bash tokenpak/companion/hooks/deleted.sh"}]}
            ]
        }
    }
    _write(tmp_path, "sample/hooks.json", json.dumps(hooks))

    findings = cvs.run(tmp_path)
    assert any("hook command references missing file" in f.message for f in findings)


def test_guard_fails_hooks_json_missing_python_module(tmp_path):
    _seed_required(tmp_path)
    hooks = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"command": "python -m tokenpak.companion.codex.deleted"}]}
            ]
        }
    }
    _write(tmp_path, "sample/hooks.json", json.dumps(hooks))

    findings = cvs.run(tmp_path)
    assert any("tokenpak/companion/codex/deleted.py" in f.message for f in findings)


def test_guard_fails_dangling_literal_module_reference(tmp_path):
    _seed_required(tmp_path)
    _write(tmp_path, "docs/ref.md", "Use tokenpak.companion.codex.deleted in examples.\n")

    findings = cvs.run(tmp_path)
    assert any("dangling companion codex module reference" in f.message for f in findings)
