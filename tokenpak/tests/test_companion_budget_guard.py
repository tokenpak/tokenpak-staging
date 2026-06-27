# SPDX-License-Identifier: Apache-2.0
"""End-to-end guard tests for the companion pre-send budget gate.

The live ``UserPromptSubmit`` hook is ``companion/hooks/pre_send.sh`` (the
launcher prefers it over the Python hook). These tests drive that bash hook as
a subprocess — exactly as Claude Code invokes it — and assert the packet's
acceptance criteria:

  * an over-budget prompt is BLOCKED before send (exit 2 + ``decision: block``
    JSON), demonstrated by a test, not just by config;
  * the companion cost ledger (``companion_costs``) is FED by the live hook
    path, with no dependency on the unused Python hook being manually wired;
  * the SQLite dependency is resolved via the bundled Python ``sqlite3`` module,
    NOT the host ``sqlite3`` CLI — proven by enforcing the budget with a broken
    ``sqlite3`` shadowed onto ``PATH``;
  * the no-budget fast path does not touch the ledger.

Maps to p2-companion-budget-enforcement-and-session-id-runtime-fix-2026-06-19.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "companion" / "hooks" / "pre_send.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash required to drive the live hook"
)


def _journal_dir(tmp_path: Path) -> Path:
    d = tmp_path / "companion"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_hook(
    tmp_path: Path,
    budget: str | None,
    input_obj: dict,
    *,
    extra_path: str | None = None,
) -> subprocess.CompletedProcess:
    """Invoke the live bash hook with the given stdin payload and env."""
    env = dict(os.environ)
    env["TOKENPAK_COMPANION_JOURNAL_DIR"] = str(_journal_dir(tmp_path))
    # Pin the interpreter the budget path uses (as the launcher does).
    env["TOKENPAK_COMPANION_PYTHON"] = sys.executable
    env.pop("TOKENPAK_COMPANION_ENABLED", None)
    if budget is None:
        env.pop("TOKENPAK_COMPANION_BUDGET", None)
    else:
        env["TOKENPAK_COMPANION_BUDGET"] = budget
    if extra_path is not None:
        env["PATH"] = extra_path + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps(input_obj),
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )


def _seed_ledger(tmp_path: Path, input_tokens: int) -> None:
    """Record a cost row for today via the canonical BudgetTracker schema."""
    from tokenpak.companion.budget.tracker import BudgetTracker

    db = _journal_dir(tmp_path) / "budget.db"
    BudgetTracker(db_path=db, daily_budget=0.0).record(
        input_tokens=input_tokens, session_id="seed"
    )


def _ledger_total(tmp_path: Path) -> float:
    db = _journal_dir(tmp_path) / "budget.db"
    if not db.exists():
        return 0.0
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(estimated_cost), 0) FROM companion_costs"
        ).fetchone()
        return float(row[0]) if row else 0.0
    finally:
        conn.close()


def _ledger_rows(tmp_path: Path) -> int:
    db = _journal_dir(tmp_path) / "budget.db"
    if not db.exists():
        return 0
    conn = sqlite3.connect(str(db))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM companion_costs").fetchone()[0])
    finally:
        conn.close()


def _transcript(tmp_path: Path, size_bytes: int = 4000) -> str:
    t = tmp_path / "transcript.jsonl"
    t.write_text("x" * size_bytes)
    return str(t)


class TestBudgetGate:
    def test_over_budget_blocks_before_send(self, tmp_path):
        """Accumulated daily spend already over budget → block (exit 2)."""
        _seed_ledger(tmp_path, input_tokens=5_000_000)
        total = _ledger_total(tmp_path)
        assert total > 0.0
        budget = f"{total / 2:.6f}"  # accumulated alone already exceeds budget
        proc = _run_hook(
            tmp_path,
            budget=budget,
            input_obj={
                "session_id": "s1",
                "transcript_path": _transcript(tmp_path),
                "prompt": "hi",
            },
        )
        assert proc.returncode == 2, proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert payload["hookSpecificOutput"]["decision"] == "block"

    def test_under_budget_allows(self, tmp_path):
        proc = _run_hook(
            tmp_path,
            budget="100.00",
            input_obj={
                "session_id": "s2",
                "transcript_path": _transcript(tmp_path),
                "prompt": "hi",
            },
        )
        assert proc.returncode == 0, proc.stderr

    def test_live_hook_feeds_ledger(self, tmp_path):
        """An allowed prompt records a companion_costs row (ledger is fed by
        the live path — no manual wiring of the Python hook)."""
        before = _ledger_rows(tmp_path)
        proc = _run_hook(
            tmp_path,
            budget="100.00",
            input_obj={
                "session_id": "s3",
                "transcript_path": _transcript(tmp_path),
                "prompt": "hi",
            },
        )
        assert proc.returncode == 0, proc.stderr
        assert _ledger_rows(tmp_path) == before + 1

    def test_no_budget_does_not_touch_ledger(self, tmp_path):
        """No budget set → exit 0 and the fast path does not write the ledger
        (preserves the pure-bash fast path for the common case)."""
        proc = _run_hook(
            tmp_path,
            budget=None,
            input_obj={
                "session_id": "s4",
                "transcript_path": _transcript(tmp_path),
                "prompt": "hi",
            },
        )
        assert proc.returncode == 0, proc.stderr
        assert _ledger_rows(tmp_path) == 0

    def test_enforces_with_broken_sqlite3_cli(self, tmp_path):
        """Budget is enforced via the bundled Python sqlite3 module even when
        the host ``sqlite3`` CLI is broken/absent. A gate that trusted a broken
        CLI would read 0.0 and fail open; the helper falls back to Python."""
        _seed_ledger(tmp_path, input_tokens=5_000_000)
        total = _ledger_total(tmp_path)
        budget = f"{total / 2:.6f}"
        stub_dir = tmp_path / "stubbin"
        stub_dir.mkdir()
        stub = stub_dir / "sqlite3"
        stub.write_text("#!/usr/bin/env bash\nexit 1\n")
        stub.chmod(0o755)
        proc = _run_hook(
            tmp_path,
            budget=budget,
            input_obj={
                "session_id": "s5",
                "transcript_path": _transcript(tmp_path),
                "prompt": "hi",
            },
            extra_path=str(stub_dir),
        )
        assert proc.returncode == 2, proc.stderr
