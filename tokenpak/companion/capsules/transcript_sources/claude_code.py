"""Claude Code transcript source for companion memory capsules.

This module contains the Claude Code storage knowledge that capsule generation
must not embed. It discovers recently modified transcript files under the
Claude Code projects directory and normalizes parsed entries into the generic
``Message`` contract.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

from tokenpak.companion.transcript.parser import parse_transcript

from .base import Message


class ClaudeCodeTranscriptSource:
    """Load messages from Claude Code project transcript files.

    Args:
        projects_root: Optional override for tests. Defaults to the user's
            Claude Code projects directory.
    """

    source_name = "claude-code"

    def __init__(self, projects_root: Path | str | None = None) -> None:
        self._projects_root = Path(projects_root).expanduser() if projects_root else (
            Path.home() / ".claude" / "projects"
        )

    def list_sessions(self, since: datetime) -> Iterable[str]:
        """Return the newest active session for each Claude Code project.

        Preserves the legacy daily build behavior: scan transcript files touched
        since the cutoff, sort newest first, and keep only the newest session per
        project directory to avoid generating many near-duplicate capsules.
        """
        if not self._projects_root.exists():
            return []

        cutoff = since.timestamp()
        candidates: list[tuple[float, Path, str]] = []
        for project_dir in self._projects_root.glob("*"):
            if not project_dir.is_dir():
                continue
            for transcript_path in project_dir.glob("*.jsonl"):
                try:
                    mtime = transcript_path.stat().st_mtime
                except OSError:
                    continue
                if mtime >= cutoff:
                    candidates.append((mtime, transcript_path, project_dir.name))

        seen_projects: set[str] = set()
        sessions: list[str] = []
        for _mtime, transcript_path, project_name in sorted(candidates, reverse=True):
            if project_name in seen_projects:
                continue
            seen_projects.add(project_name)
            sessions.append(transcript_path.stem)
        return sessions

    def load_messages(self, session_id: str) -> Iterable[Message]:
        """Return normalized messages for ``session_id`` or ``[]`` if absent."""
        transcript_path = self._find_session_path(session_id)
        if transcript_path is None:
            return []

        summary = parse_transcript(transcript_path)
        return [
            Message(
                role=message.role or message.type,
                content=message.content,
                timestamp=message.timestamp,
                metadata={"type": message.type, "source_path": str(transcript_path)},
            )
            for message in summary.messages
            if message.content
        ]

    def _find_session_path(self, session_id: str) -> Path | None:
        if not session_id or not self._projects_root.exists():
            return None
        for project_dir in self._projects_root.glob("*"):
            if not project_dir.is_dir():
                continue
            candidate = project_dir / f"{session_id}.jsonl"
            if candidate.exists():
                return candidate
        return None
