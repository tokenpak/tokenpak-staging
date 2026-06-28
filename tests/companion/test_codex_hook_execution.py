"""Windows-native execution tests for the Codex companion hooks.

Covers audit findings CP-01 (Pythonize the Codex hook command path) and
CP-06 (parameterized, cross-platform SQLite writes).

Unlike ``test_codex_hooks.py`` — which exercises the legacy POSIX ``.sh``
path through ``bash`` — every test here launches the Python-native hook with
``sys.executable`` and feeds JSON on stdin. No test assumes ``/bin/bash``,
``jq``, ``sqlite3``, ``bc``, ``sed``, or ``cut`` is installed, so the same
assertions hold on native Windows PowerShell/cmd.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

from tokenpak.companion.codex import hooks as codex_hooks

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CODEX_DIR = _REPO_ROOT / "tokenpak" / "companion" / "codex"

_SESSION_START = _CODEX_DIR / "hooks_session_start.py"
_PRE_SEND = _CODEX_DIR / "hooks_pre_send.py"
_PRE_TOOL_USE = _CODEX_DIR / "hooks_pre_tool_use.py"
_POST_TOOL_USE = _CODEX_DIR / "hooks_post_tool_use.py"
_STOP = _CODEX_DIR / "hooks_stop.py"

# Values that break naive shell/SQL string interpolation. Each Codex payload
# field is exercised with these to prove the Python hooks never shell out and
# always parameterize their SQLite writes (CP-06).
_SPACE = "a path with spaces"
_BACKSLASH = r"C:\Users\dev\proj"
_QUOTE = "sess'with\"quotes"


def _run(
    script: Path,
    payload: dict,
    tmp_path: Path,
    env_extra: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run a hook via the current interpreter — never via bash."""
    import os

    env = os.environ.copy()
    env["TOKENPAK_COMPANION_ENABLED"] = "1"
    env["TOKENPAK_COMPANION_JOURNAL_DIR"] = str(tmp_path)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )


def _seed_journal_db(tmp_path: Path) -> Path:
    db = tmp_path / "journal.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            started_at REAL NOT NULL,
            ended_at REAL,
            project_dir TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            total_requests INTEGER NOT NULL DEFAULT 0,
            total_cost_usd REAL NOT NULL DEFAULT 0.0,
            total_input_tokens INTEGER NOT NULL DEFAULT 0,
            total_output_tokens INTEGER NOT NULL DEFAULT 0,
            capsule_path TEXT
        );
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            timestamp REAL NOT NULL,
            entry_type TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    conn.commit()
    conn.close()
    return db


def _seed_budget_db(tmp_path: Path, daily_cost: float = 0.0) -> Path:
    db = tmp_path / "budget.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS companion_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            date TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            cached_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost REAL NOT NULL DEFAULT 0.0
        )
        """
    )
    if daily_cost:
        conn.execute(
            "INSERT INTO companion_costs (timestamp, date, estimated_cost) VALUES (?, ?, ?)",
            (0.0, date.today().isoformat(), daily_cost),
        )
    conn.commit()
    conn.close()
    return db


def _entries(db: Path, session_id: str) -> list[str]:
    conn = sqlite3.connect(str(db))
    try:
        return [
            r[0]
            for r in conn.execute(
                "SELECT content FROM entries WHERE session_id = ?", (session_id,)
            ).fetchall()
        ]
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────
# CP-01 — generate_hooks_json emits Python-native commands.
# ──────────────────────────────────────────────────────────────


def test_generated_commands_invoke_interpreter_not_shell_tools():
    """No installed Codex hook command requires bash/jq/sed/sqlite3/bc/cut.

    The program token must be the companion interpreter (``sys.executable``),
    never a bare ``python3`` / ``bash`` literal that depends on PATH.
    """
    hooks_json = codex_hooks.generate_hooks_json()
    forbidden_tokens = {"bash", "jq", "sed", "sqlite3", "bc", "cut", "python3"}
    for event, groups in hooks_json["hooks"].items():
        for group in groups:
            for cmd in group["hooks"]:
                command = cmd["command"]
                tokens = command.split()
                program = tokens[0]
                assert program == sys.executable, f"{event}: program {program!r}"
                # No forbidden tool appears as a whitespace-delimited token.
                assert forbidden_tokens.isdisjoint(tokens), f"{event}: {command!r}"
                # The script target is a .py hook, not a .sh.
                assert tokens[1].endswith(".py"), f"{event}: {command!r}"


def test_generated_commands_marker_preserved():
    """doctor/uninstall identify tokenpak hooks by the ``tokenpak`` marker."""
    hooks_json = codex_hooks.generate_hooks_json()
    for event, groups in hooks_json["hooks"].items():
        for group in groups:
            for cmd in group["hooks"]:
                assert codex_hooks.TOKENPAK_HOOK_MARKER in cmd["command"], event


# ──────────────────────────────────────────────────────────────
# CP-01 — SessionStart.
# ──────────────────────────────────────────────────────────────


def test_session_start_emits_banner_and_writes_marker(tmp_path):
    payload = {
        "session_id": "exec-session-start",
        "cwd": "/tmp/x",
        "model": "gpt-5",
        "source": "startup",
        "hook_event_name": "SessionStart",
    }
    result = _run(_SESSION_START, payload, tmp_path)
    assert result.returncode == 0
    assert "tokenpak" in result.stderr.lower()
    assert "startup" in result.stderr.lower()
    assert (tmp_path / "run" / "current-session").read_text().strip() == "exec-session-start"


def test_session_start_journals_when_db_present(tmp_path):
    db = _seed_journal_db(tmp_path)
    payload = {
        "session_id": "exec-ss-journal",
        "cwd": "/tmp/x",
        "model": "gpt-5",
        "source": "resume",
        "hook_event_name": "SessionStart",
    }
    result = _run(_SESSION_START, payload, tmp_path)
    assert result.returncode == 0
    contents = _entries(db, "exec-ss-journal")
    assert any("session started" in c for c in contents)
    assert any("resume" in c for c in contents)


def test_session_start_surfaces_prior_capsule(tmp_path):
    db = _seed_journal_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO sessions (session_id, started_at, project_dir, capsule_path) "
        "VALUES (?, ?, ?, ?)",
        ("prior", 1.0, "/tmp/proj", "/tmp/cap.json"),
    )
    conn.commit()
    conn.close()
    payload = {
        "session_id": "exec-ss-capsule",
        "cwd": "/tmp/proj",
        "model": "gpt-5",
        "source": "resume",
        "hook_event_name": "SessionStart",
    }
    result = _run(_SESSION_START, payload, tmp_path)
    assert result.returncode == 0
    out = json.loads(result.stdout.strip())
    assert out["systemMessage"].endswith("/tmp/cap.json")
    assert out["continue"] is True


# ──────────────────────────────────────────────────────────────
# CP-01 — UserPromptSubmit.
# ──────────────────────────────────────────────────────────────


def test_pre_send_prints_cost_estimate(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(b"x" * 20_000)
    rates = tmp_path / "rates.tsv"
    rates.write_text("cheap\t0.8\n")
    payload = {"session_id": "s", "transcript_path": str(transcript), "model": "cheap"}
    result = _run(
        _PRE_SEND, payload, tmp_path, env_extra={"TOKENPAK_COMPANION_RATES_FILE": str(rates)}
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""  # nothing blocks
    # 5,000 tokens * 0.8 / 1e6 = 0.004
    assert "est $0.004000" in result.stderr
    assert "5,000 tokens" in result.stderr


def test_pre_send_blocks_when_over_budget(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(b"x" * 200_000)
    _seed_budget_db(tmp_path, daily_cost=1.0)
    payload = {"session_id": "s", "transcript_path": str(transcript), "model": "sonnet"}
    result = _run(
        _PRE_SEND, payload, tmp_path, env_extra={"TOKENPAK_COMPANION_BUDGET": "0.0001"}
    )
    assert result.returncode == 2, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    spec = json.loads(result.stdout.strip())["hookSpecificOutput"]
    assert spec["hookEventName"] == "UserPromptSubmit"
    assert spec["decision"] == "block"
    assert "budget" in spec["reason"].lower()
    assert "budget" in result.stderr.lower()


def test_pre_send_no_transcript_is_noop(tmp_path):
    payload = {"session_id": "s", "transcript_path": "", "model": "sonnet"}
    result = _run(_PRE_SEND, payload, tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


# ──────────────────────────────────────────────────────────────
# CP-01 — PreToolUse.
# ──────────────────────────────────────────────────────────────


def test_pre_tool_use_allows_and_stamps(tmp_path):
    db = _seed_journal_db(tmp_path)
    payload = {
        "session_id": "exec-pre-tool",
        "tool_name": "Bash",
        "tool_use_id": "u1",
        "turn_id": "t1",
        "hook_event_name": "PreToolUse",
    }
    result = _run(_PRE_TOOL_USE, payload, tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert any("pre_tool" in c for c in _entries(db, "exec-pre-tool"))


def test_pre_tool_use_blocks_when_over_budget(tmp_path):
    _seed_journal_db(tmp_path)
    _seed_budget_db(tmp_path, daily_cost=1.0)
    payload = {
        "session_id": "exec-pre-tool-block",
        "tool_name": "Bash",
        "tool_use_id": "u2",
        "turn_id": "t2",
        "hook_event_name": "PreToolUse",
    }
    result = _run(
        _PRE_TOOL_USE, payload, tmp_path, env_extra={"TOKENPAK_COMPANION_BUDGET": "0.5"}
    )
    assert result.returncode == 2, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    spec = json.loads(result.stdout.strip())["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    assert spec["permissionDecision"] == "deny"
    assert "budget" in spec["permissionDecisionReason"].lower()


# ──────────────────────────────────────────────────────────────
# CP-01 — PostToolUse.
# ──────────────────────────────────────────────────────────────


def test_post_tool_use_journals_token_out(tmp_path):
    db = _seed_journal_db(tmp_path)
    payload = {
        "session_id": "exec-post-tool",
        "tool_name": "Bash",
        "tool_use_id": "u3",
        "turn_id": "t3",
        "tool_response": "hello world",
        "hook_event_name": "PostToolUse",
    }
    result = _run(_POST_TOOL_USE, payload, tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    contents = _entries(db, "exec-post-tool")
    assert any("post_tool" in c and "tokens out" in c for c in contents)


def test_post_tool_use_hardcap_emits_additional_context(tmp_path):
    _seed_journal_db(tmp_path)
    payload = {
        "session_id": "exec-post-hardcap",
        "tool_name": "Bash",
        "tool_use_id": "u4",
        "turn_id": "t4",
        "tool_response": "y" * 400,
        "hook_event_name": "PostToolUse",
    }
    result = _run(
        _POST_TOOL_USE,
        payload,
        tmp_path,
        env_extra={"TOKENPAK_COMPANION_RESPONSE_HARDCAP_TOKENS": "10"},
    )
    assert result.returncode == 0
    spec = json.loads(result.stdout.strip())["hookSpecificOutput"]
    assert spec["hookEventName"] == "PostToolUse"
    assert "hard cap" in spec["additionalContext"].lower()


# ──────────────────────────────────────────────────────────────
# CP-01 — Stop.
# ──────────────────────────────────────────────────────────────


def test_stop_records_decimal_safe_cost(tmp_path):
    _seed_journal_db(tmp_path)
    budget_db = _seed_budget_db(tmp_path)
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(b"x" * 800_000)  # 200,000 tokens
    rates = tmp_path / "rates.tsv"
    rates.write_text("sonnet\t3\n")
    payload = {
        "session_id": "exec-stop",
        "transcript_path": str(transcript),
        "model": "sonnet",
        "hook_event_name": "Stop",
    }
    result = _run(
        _STOP, payload, tmp_path, env_extra={"TOKENPAK_COMPANION_RATES_FILE": str(rates)}
    )
    assert result.returncode == 0
    conn = sqlite3.connect(str(budget_db))
    try:
        row = conn.execute(
            "SELECT input_tokens, estimated_cost FROM companion_costs WHERE session_id = ?",
            ("exec-stop",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == 200_000
    # 200,000 * 3 / 1e6 = 0.6 — NOT 0.0006 (the bc decimal-truncation bug).
    assert abs(row[1] - 0.6) < 1e-9


# ──────────────────────────────────────────────────────────────
# CP-06 — values with spaces, backslashes, and quotes are parameterized.
# ──────────────────────────────────────────────────────────────


def test_session_id_with_quotes_spaces_backslashes_is_parameterized(tmp_path):
    db = _seed_journal_db(tmp_path)
    weird_id = f"{_QUOTE} {_SPACE} {_BACKSLASH}"
    payload = {
        "session_id": weird_id,
        "cwd": _BACKSLASH,
        "model": "m'odel",
        "source": "startup",
        "hook_event_name": "SessionStart",
    }
    result = _run(_SESSION_START, payload, tmp_path)
    assert result.returncode == 0
    # Stored verbatim — proves the value was bound, not interpolated.
    assert _entries(db, weird_id), "weird session_id should be stored literally"
    assert (tmp_path / "run" / "current-session").read_text().strip() == weird_id


def test_stop_cwd_and_model_with_quotes_do_not_break_sql(tmp_path):
    _seed_journal_db(tmp_path)
    budget_db = _seed_budget_db(tmp_path)
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(b"x" * 4_000)
    payload = {
        "session_id": _QUOTE,
        "transcript_path": str(transcript),
        "model": "model'; DROP TABLE companion_costs;--",
        "hook_event_name": "Stop",
    }
    result = _run(_STOP, payload, tmp_path)
    assert result.returncode == 0
    conn = sqlite3.connect(str(budget_db))
    try:
        # Table survives — the injection payload was bound, not executed.
        rows = conn.execute(
            "SELECT session_id, model FROM companion_costs WHERE session_id = ?", (_QUOTE,)
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(_QUOTE, "model'; DROP TABLE companion_costs;--")]


def test_companion_disabled_short_circuits(tmp_path):
    _seed_journal_db(tmp_path)
    payload = {"session_id": "disabled", "cwd": "/tmp", "source": "startup"}
    result = _run(
        _SESSION_START, payload, tmp_path, env_extra={"TOKENPAK_COMPANION_ENABLED": "0"}
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert result.stderr.strip() == ""


def test_unparseable_stdin_fails_open(tmp_path):
    import os

    env = os.environ.copy()
    env["TOKENPAK_COMPANION_JOURNAL_DIR"] = str(tmp_path)
    for script in (_SESSION_START, _PRE_SEND, _PRE_TOOL_USE, _POST_TOOL_USE, _STOP):
        result = subprocess.run(
            [sys.executable, str(script)],
            input="not json {",
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
        assert result.returncode == 0, f"{script.name} should fail open"
