# SPDX-License-Identifier: Apache-2.0
"""Record native Codex lifecycle metadata without an external sqlite3 command.

Only turn identity/model/timestamps are retained. Native transcripts remain the
source of conversation text; this writer never fabricates token or cost facts.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import sys
import time
from datetime import datetime
from pathlib import Path
from uuid import UUID

# Shell hooks execute this packaged file with the launcher's interpreter. Bind
# imports to the same installation even when the caller's cwd contains a repo.
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tokenpak.companion import _sqlite
from tokenpak.companion.config import journal_write_dir
from tokenpak.status.binding import valid_session

MAX_TRANSCRIPT_BYTES = 1024 * 1024 * 1024
MAX_LINE_BYTES = 4 * 1024 * 1024
# Native content records carry no journal identity. Recognize only the native
# header shape; unknown shapes still go through the bounded JSON parser.
_CONTENT_RECORD = re.compile(
    rb'^\s*\{(?:\s*"(?:timestamp|ordinal)"\s*:\s*(?:"[^"\\]*"|[0-9]+)\s*,)*'
    rb'\s*"type"\s*:\s*"(response_item|event_msg)"\s*,'
    rb'\s*"payload"\s*:\s*\{\s*"type"\s*:\s*"([^"\\]+)"'
)


def _starts_in_session(turn_id: str, native_start: object, session_start: float) -> bool:
    """Resolve coarse native start seconds using UUIDv7's millisecond time."""
    try:
        identity = UUID(turn_id)
        if identity.version == 7:
            return (identity.int >> 80) / 1000 >= session_start
    except ValueError:
        pass
    return (
        isinstance(native_start, (int, float))
        and not isinstance(native_start, bool)
        and native_start >= session_start
    )


def transcript_turns(path: Path, session_id: str) -> tuple[list[dict], dict]:
    """Read one explicit native transcript, refusing cross-session imports."""
    turns: list[dict] = []
    current: dict = {}
    matched = False
    started_at = None
    ancestors: set[str] = set()
    forked = False
    own_turns: set[str] = set()
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Codex transcript must be a regular file")
        if info.st_size > MAX_TRANSCRIPT_BYTES:
            raise ValueError("Codex transcript exceeds journal recovery read limit")
        bytes_read = 0
        while raw := handle.readline(MAX_LINE_BYTES + 1):
            bytes_read += len(raw)
            if bytes_read > MAX_TRANSCRIPT_BYTES:
                raise ValueError("Codex transcript exceeds journal recovery read limit")
            content = _CONTENT_RECORD.match(raw)
            if content and (
                content[1] == b"response_item" or content[2] in {b"item_started", b"item_completed"}
            ):
                # Discard large tool/message bodies in bounded chunks. They
                # cannot register a session or certify a completed native turn.
                while not raw.endswith(b"\n"):
                    raw = handle.readline(MAX_LINE_BYTES + 1)
                    if not raw:
                        break
                    bytes_read += len(raw)
                    if bytes_read > MAX_TRANSCRIPT_BYTES:
                        raise ValueError("Codex transcript exceeds journal recovery read limit")
                continue
            if len(raw) > MAX_LINE_BYTES:
                raise ValueError("Codex transcript line exceeds journal read limit")
            if not raw.endswith(b"\n"):
                break  # The live writer may not have finished the final record.
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError("Codex transcript record must be an object")
            payload = event.get("payload", {})
            if not isinstance(payload, dict):
                continue
            kind = event.get("type")
            if kind == "session_meta":
                if payload.get("id") != session_id:
                    if matched and payload.get("id") in ancestors:
                        parent = payload.get("forked_from_id")
                        if valid_session(parent):
                            ancestors.add(parent)
                        continue  # A declared ancestor copied into a fork.
                    raise ValueError("Codex transcript session does not match hook session")
                matched = True
                parent = payload.get("forked_from_id")
                if valid_session(parent):
                    ancestors.add(parent)
                    forked = True
                if payload.get("timestamp"):
                    started_at = datetime.fromisoformat(
                        payload["timestamp"].replace("Z", "+00:00")
                    ).timestamp()
            elif matched and kind == "event_msg" and payload.get("type") == "task_started":
                # Forked logs rewrite envelope timestamps while preserving
                # native started_at. Only starts after this session's creation
                # belong to it; ambiguous/missing ancestry timing stays absent.
                native_start = payload.get("started_at")
                if (
                    valid_session(payload.get("turn_id"))
                    and started_at is not None
                    and _starts_in_session(payload["turn_id"], native_start, started_at)
                ):
                    own_turns.add(payload["turn_id"])
            elif matched and kind == "turn_context":
                if forked and payload.get("turn_id") not in own_turns:
                    current = {}
                    continue
                current = {
                    "turn_id": payload.get("turn_id", ""),
                    "model": payload.get("model", ""),
                    "cwd": payload.get("cwd", ""),
                }
            elif matched and kind == "event_msg" and payload.get("type") == "task_complete":
                turn_id = payload.get("turn_id")
                if valid_session(turn_id) and current.get("turn_id") == turn_id:
                    stamp = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
                    turns.append({**current, "timestamp": stamp.timestamp()})
    if not matched:
        raise ValueError("Codex transcript has no matching native session metadata")
    if started_at is not None:
        current["started_at"] = started_at
    return turns, current


def record(payload: dict, *, recover: bool = False) -> int:
    """Register a session and idempotently retain each observed completed turn."""
    if not isinstance(payload, dict):
        raise ValueError("Codex journal hook input must be an object")
    session_id = payload.get("session_id", "")
    if not valid_session(session_id):
        raise ValueError("Codex journal needs a valid native session_id")
    event = payload.get("hook_event_name", "")
    if not recover and event not in {"SessionStart", "Stop"}:
        raise ValueError("Codex journal needs a SessionStart or Stop event")
    turns: list[dict] = []
    current: dict = {}
    transcript = payload.get("transcript_path")
    if transcript and (recover or event == "Stop"):
        turns, current = transcript_turns(Path(transcript), session_id)
    if event == "Stop" and not recover:
        # Stop is an observed turn boundary, not the end of the whole session.
        # It can run before task_complete has been flushed to the native log.
        turn_id = payload.get("turn_id") or current.get("turn_id")
        if current.get("turn_id") and turn_id != current["turn_id"]:
            raise ValueError("Codex Stop turn does not match the native transcript")
        if not valid_session(turn_id):
            raise ValueError("Codex Stop has no native turn identity; turn was not recorded")
        if not any(turn["turn_id"] == turn_id for turn in turns):
            turns.append(
                {
                    **current,
                    "turn_id": turn_id,
                    "timestamp": time.time(),
                    "model": payload.get("model") or current.get("model", ""),
                }
            )
    directory = journal_write_dir()
    directory.mkdir(parents=True, exist_ok=True)
    connection = _sqlite.connect(directory / "journal.db")
    added = 0
    try:
        with _sqlite._write_transaction(connection):
            _sqlite.ensure_journal_schema(connection)
            connection.execute(
                "INSERT OR IGNORE INTO sessions (session_id, started_at, project_dir, model) "
                "VALUES (?, ?, ?, ?)",
                (
                    session_id,
                    current.get("started_at")
                    or min((turn["timestamp"] for turn in turns), default=time.time()),
                    payload.get("cwd") or current.get("cwd", ""),
                    payload.get("model") or current.get("model", ""),
                ),
            )
            if current.get("started_at") is not None:
                connection.execute(
                    "UPDATE sessions SET started_at = MIN(started_at, ?) WHERE session_id = ?",
                    (current["started_at"], session_id),
                )
            if event == "SessionStart" and not recover:
                metadata = json.dumps(
                    {"event": "codex_session_start", "source": payload.get("source", "startup")}
                )
                content = f"session started (source: {payload.get('source', 'startup')})"
                connection.execute(
                    "INSERT OR IGNORE INTO entries "
                    "(session_id, timestamp, entry_type, content, metadata_json, content_hash) "
                    "VALUES (?, ?, 'auto', ?, ?, ?)",
                    (
                        session_id,
                        time.time(),
                        content,
                        metadata,
                        _sqlite.entry_content_hash("auto", content, metadata),
                    ),
                )
            for turn in turns:
                metadata = json.dumps(
                    {
                        "event": "codex_turn_complete",
                        "turn_id": turn["turn_id"],
                        "model": turn.get("model", ""),
                        "source": "native_codex",
                    }
                )
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO entries "
                    "(session_id, timestamp, entry_type, content, metadata_json, content_hash) "
                    "VALUES (?, ?, 'auto', ?, ?, ?)",
                    (
                        session_id,
                        turn["timestamp"],
                        f"turn completed (model: {turn.get('model') or 'unknown'})",
                        metadata,
                        _sqlite.entry_content_hash("auto", turn["turn_id"], "codex_turn_complete"),
                    ),
                )
                added += cursor.rowcount
            # Preserve other journal writers' totals. Native turns have their
            # own marker; tool calls and repeated hook deliveries are not turns.
            connection.execute(
                "UPDATE sessions SET total_requests = MAX(total_requests, "
                "(SELECT COUNT(*) FROM entries WHERE session_id = ? "
                "AND json_extract(metadata_json, '$.event') = 'codex_turn_complete')) "
                "WHERE session_id = ?",
                (session_id, session_id),
            )
            models = {turn.get("model") for turn in turns if turn.get("model")}
            if models:
                previous = connection.execute(
                    "SELECT model FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
                if previous:
                    models.add(previous)
                connection.execute(
                    "UPDATE sessions SET model = ? WHERE session_id = ?",
                    (next(iter(models)) if len(models) == 1 else "mixed", session_id),
                )
        if turns or event == "SessionStart":
            try:
                (directory / "journal.db.nonempty").touch(exist_ok=True)
            except OSError:
                pass  # Advisory recall marker; the journal commit already succeeded.
        return added
    finally:
        connection.close()


def main() -> int:
    if os.environ.get("TOKENPAK_COMPANION_ENABLED", "1") == "0":
        return 0
    try:
        payload = json.load(sys.stdin)
        recover = "--recover" in sys.argv[1:]
        added = record(payload, recover=recover)
        if recover:
            print(json.dumps({"session_id": payload["session_id"], "recorded_turns": added}))
        return 0
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        print(f"tokenpak: Codex history was not updated: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
