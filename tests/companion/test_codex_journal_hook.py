# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tokenpak.companion.codex import journal_hook


def native_transcript(path, session="session-one", *, complete=True):
    events = [{"type": "session_meta", "payload": {"id": session}}]
    for number, model in enumerate(("model-first", "model-second")):
        events.append(
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": f"turn-{number}",
                    "model": model,
                    "cwd": "/project's directory",
                },
            }
        )
        if complete or number == 0:
            events.append(
                {
                    "type": "event_msg",
                    "timestamp": "2026-09-08T12:00:00Z",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": f"turn-{number}",
                        "last_agent_message": "private conversation text",
                    },
                }
            )
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


def test_stop_registers_native_models_and_turns_without_sqlite_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_COMPANION_JOURNAL_DIR", str(tmp_path / "journal"))
    path = native_transcript(tmp_path / "native.jsonl", complete=False)
    payload = {"session_id": "session-one", "transcript_path": str(path), "hook_event_name": "Stop"}
    assert journal_hook.record(payload) == 2
    assert journal_hook.record(payload) == 0
    with sqlite3.connect(tmp_path / "journal/journal.db") as db:
        assert db.execute(
            "SELECT model,total_requests,ended_at,project_dir FROM sessions"
        ).fetchone() == ("mixed", 2, None, "/project's directory")
        entries = db.execute("SELECT metadata_json,content FROM entries").fetchall()
        assert {json.loads(row[0])["turn_id"] for row in entries} == {"turn-0", "turn-1"}
        assert "private conversation text" not in str(entries)


def test_recovery_preserves_unfinished_turn_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_COMPANION_JOURNAL_DIR", str(tmp_path / "journal"))
    path = native_transcript(tmp_path / "native.jsonl", complete=False)
    payload = {"session_id": "session-one", "transcript_path": str(path)}
    assert journal_hook.record(payload, recover=True) == 1
    assert journal_hook.record(payload, recover=True) == 0
    native_transcript(path)
    assert journal_hook.record(payload, recover=True) == 1


def test_wrong_session_refuses_without_creating_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_COMPANION_JOURNAL_DIR", str(tmp_path / "journal"))
    path = native_transcript(tmp_path / "native.jsonl", session="another-session")
    with pytest.raises(ValueError, match="does not match"):
        journal_hook.record(
            {"session_id": "session-one", "transcript_path": str(path)}, recover=True
        )
    assert not (tmp_path / "journal").exists()


def test_repeated_prompt_content_does_not_collapse_distinct_turns(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_COMPANION_JOURNAL_DIR", str(tmp_path / "journal"))
    for turn_id in ("turn-first", "turn-second", "turn-second"):
        journal_hook.record(
            {
                "session_id": "session-one",
                "hook_event_name": "Stop",
                "turn_id": turn_id,
                "model": "model-first",
            }
        )
    with sqlite3.connect(tmp_path / "journal/journal.db") as db:
        assert db.execute("SELECT total_requests FROM sessions").fetchone() == (2,)


def test_shell_hooks_work_without_external_sqlite_and_clear_stays_quiet(tmp_path):
    hooks = Path(journal_hook.__file__).parent
    env = {
        **os.environ,
        "TOKENPAK_COMPANION_JOURNAL_DIR": str(tmp_path / "journal"),
        "TOKENPAK_COMPANION_PYTHON": sys.executable,
        "TOKENPAK_COMPANION_ENABLED": "1",
        "TOKENPAK_COMPANION_SHOW_COST": "0",
    }
    payload = {"session_id": "session-one", "hook_event_name": "SessionStart", "source": "clear"}
    result = subprocess.run(
        ["bash", str(hooks / "hooks_session_start.sh")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    payload.update(
        hook_event_name="Stop", transcript_path=str(native_transcript(tmp_path / "native.jsonl"))
    )
    result = subprocess.run(
        ["bash", str(hooks / "hooks_stop.sh")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    with sqlite3.connect(tmp_path / "journal/journal.db") as db:
        assert db.execute("SELECT total_requests FROM sessions").fetchone() == (2,)


def test_partial_native_record_is_not_a_completed_turn(tmp_path):
    path = native_transcript(tmp_path / "native.jsonl", complete=False)
    with path.open("ab") as handle:
        handle.write(b'{"type":"event_msg"')
    turns, current = journal_hook.transcript_turns(path, "session-one")
    assert len(turns) == 1
    assert current["turn_id"] == "turn-1"


def test_stop_refuses_a_different_native_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_COMPANION_JOURNAL_DIR", str(tmp_path / "journal"))
    path = native_transcript(tmp_path / "native.jsonl")
    with pytest.raises(ValueError, match="turn does not match"):
        journal_hook.record(
            {
                "session_id": "session-one",
                "hook_event_name": "Stop",
                "transcript_path": str(path),
                "turn_id": "unrelated-turn",
            }
        )
    assert not (tmp_path / "journal").exists()


@pytest.mark.parametrize("bad_record", ["[]\n", "{broken}\n"])
def test_malformed_complete_record_refuses_before_writing(tmp_path, monkeypatch, bad_record):
    monkeypatch.setenv("TOKENPAK_COMPANION_JOURNAL_DIR", str(tmp_path / "journal"))
    path = native_transcript(tmp_path / "native.jsonl")
    with path.open("a") as handle:
        handle.write(bad_record)
    with pytest.raises(ValueError):
        journal_hook.record(
            {"session_id": "session-one", "transcript_path": str(path)}, recover=True
        )
    assert not (tmp_path / "journal").exists()


def test_transcript_read_is_bounded(tmp_path, monkeypatch):
    path = native_transcript(tmp_path / "native.jsonl")
    monkeypatch.setattr(journal_hook, "MAX_TRANSCRIPT_BYTES", 10)
    with pytest.raises(ValueError, match="read limit"):
        journal_hook.transcript_turns(path, "session-one")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFO")
def test_transcript_fifo_refuses_without_waiting_for_a_writer(tmp_path):
    path = tmp_path / "fifo"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular file"):
        journal_hook.transcript_turns(path, "session-one")


def test_native_start_and_existing_accounting_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_COMPANION_JOURNAL_DIR", str(tmp_path / "journal"))
    journal_hook.record({"session_id": "session-one", "hook_event_name": "SessionStart"})
    with sqlite3.connect(tmp_path / "journal/journal.db") as db:
        db.execute("UPDATE sessions SET total_requests=19,total_cost_usd=1.5,total_input_tokens=42")
    path = native_transcript(tmp_path / "native.jsonl")
    lines = path.read_text().splitlines()
    meta = json.loads(lines[0])
    meta["payload"]["timestamp"] = "2026-09-08T11:00:00Z"
    lines[0] = json.dumps(meta)
    path.write_text("\n".join(lines) + "\n")
    journal_hook.record({"session_id": "session-one", "transcript_path": str(path)}, recover=True)
    with sqlite3.connect(tmp_path / "journal/journal.db") as db:
        assert db.execute(
            "SELECT total_requests,total_cost_usd,total_input_tokens FROM sessions"
        ).fetchone() == (19, 1.5, 42)
        assert db.execute("SELECT datetime(started_at,'unixepoch') FROM sessions").fetchone() == (
            "2026-09-08 11:00:00",
        )


def test_concurrent_recovery_keeps_one_entry_per_native_turn(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setenv("TOKENPAK_COMPANION_JOURNAL_DIR", str(tmp_path / "journal"))
    path = native_transcript(tmp_path / "native.jsonl")
    payload = {"session_id": "session-one", "transcript_path": str(path)}
    with ThreadPoolExecutor(max_workers=4) as executor:
        added = list(executor.map(lambda _: journal_hook.record(payload, recover=True), range(4)))
    assert sum(added) == 2
    with sqlite3.connect(tmp_path / "journal/journal.db") as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM entries").fetchone() == (2,)


def test_same_native_turn_id_in_distinct_sessions_is_not_collapsed(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_COMPANION_JOURNAL_DIR", str(tmp_path / "journal"))
    for sid in ("session-one", "session-two"):
        assert (
            journal_hook.record(
                {
                    "session_id": sid,
                    "hook_event_name": "Stop",
                    "turn_id": "turn-shared",
                    "model": "model-one",
                }
            )
            == 1
        )


def test_recovery_cli_reports_added_count_without_conversation_text(tmp_path):
    path = native_transcript(tmp_path / "native.jsonl")
    payload = json.dumps({"session_id": "session-one", "transcript_path": str(path)})
    env = {
        **os.environ,
        "TOKENPAK_COMPANION_JOURNAL_DIR": str(tmp_path / "journal"),
        "TOKENPAK_COMPANION_ENABLED": "1",
    }
    for expected in (2, 0):
        result = subprocess.run(
            [sys.executable, journal_hook.__file__, "--recover"],
            input=payload,
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        assert json.loads(result.stdout) == {
            "session_id": "session-one",
            "recorded_turns": expected,
        }
        assert result.stderr == ""


def test_declared_fork_ancestry_does_not_import_parent_turns(tmp_path):
    events = [
        {
            "type": "session_meta",
            "payload": {
                "id": "child",
                "forked_from_id": "parent",
                "timestamp": "2026-09-08T12:00:00Z",
            },
        },
        {"type": "session_meta", "payload": {"id": "parent"}},
    ]
    for turn, start in [("parent-turn", 1788868790), ("child-turn", 1788868801)]:
        events.extend(
            [
                {
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": turn, "started_at": start},
                },
                {"type": "turn_context", "payload": {"turn_id": turn, "model": "model-one"}},
                {
                    "type": "event_msg",
                    "timestamp": "2026-09-08T12:01:00Z",
                    "payload": {"type": "task_complete", "turn_id": turn},
                },
            ]
        )
    path = tmp_path / "fork.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    turns, current = journal_hook.transcript_turns(path, "child")
    assert [turn["turn_id"] for turn in turns] == ["child-turn"]
    assert current["turn_id"] == "child-turn"
    with pytest.raises(ValueError, match="does not match"):
        journal_hook.transcript_turns(path, "parent")


def test_uuid_start_disambiguates_turns_started_in_the_fork_second():
    # Native start seconds are coarse; the UUIDv7 retains milliseconds.
    assert journal_hook._starts_in_session(
        "019f4d57-8864-7ef2-a5e3-6fb495c9fcb9", 1783709009, 1783709010.01
    )
    assert not journal_hook._starts_in_session(
        "019f4d57-8864-7ef2-a5e3-6fb495c9fcb9", 1783709011, 1783709010.2
    )


def test_large_native_content_is_skipped_without_losing_completed_turns(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_hook, "MAX_LINE_BYTES", 1024)
    path = native_transcript(tmp_path / "native.jsonl")
    content = {
        "timestamp": "2026-09-08T12:00:00Z",
        "ordinal": 4,
        "type": "event_msg",
        "payload": {"type": "item_completed", "item": "private" * 1024},
    }
    lines = path.read_text().splitlines(keepends=True)
    lines.insert(2, json.dumps(content) + "\n")
    path.write_text("".join(lines))
    turns, _ = journal_hook.transcript_turns(path, "session-one")
    assert len(turns) == 2
    assert "private" not in str(turns)
