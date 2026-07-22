"""Workflow-step ratchet uniqueness regressions."""

from __future__ import annotations

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SNAPSHOT = _REPO_ROOT / "tokenpak" / "_snapshots" / "workflow-steps.json"


def test_each_guarded_workflow_step_is_recorded_once():
    """Overlapping guarded globs must not duplicate one workflow's steps."""
    snapshot = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    rows = [(row["workflow"], row["job"], row["id"], row["name"]) for row in snapshot["steps"]]
    assert len(rows) == len(set(rows)), "workflow-step snapshot contains duplicate rows"
