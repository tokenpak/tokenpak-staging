# SPDX-License-Identifier: Apache-2.0
"""gstack TIP source adapter — first ``ExternalToolTIPSource`` instance.

Observes **gstack** (an MIT-licensed sprint/role agent toolkit driven by
slash commands) through the Claude Code transcript surface and projects
detected invocations into TokenPak-observed TIP records (Std 23 §9).

What it reads (READ-ONLY — Std 23 §9.3(3); packet AC #6):

* Claude Code session transcripts under the standard projects root
  (same dynamic discovery as
  :mod:`tokenpak.vault.sources.claude_transcript` — honours
  ``CLAUDE_PROJECTS_DIR``, defaults to ``~/.claude/projects``).
* Optionally, JSON state files under ``GSTACK_STATE_ROOT`` for
  best-effort role/phase enrichment.  Never required, never written.

What it emits: one :class:`ObservedTIPRecord` per detected gstack
invocation span, carrying the detected command / role / phase plus the
**companion-observed** token counters summed over the span, labeled
under ``ext.gstack.*`` (never ``tip.*``).

Version tolerance (packet AC #1): there is **no hardcoded enumeration**
of gstack commands, roles, or phases — whatever token gstack's current
version uses is captured dynamically and sanitized into a label.
Unknown / malformed shapes degrade gracefully: skip + structured log
line, never a crash.

OFF BY DEFAULT — runs only via the ``TOKENPAK_TIP_TOOL_ADAPTERS`` gate
in :mod:`tokenpak.sources.external_tool_tip` (no daemon; on-demand
batch over the transcript, packet AC #2).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from tokenpak.sources.external_tool_tip import (
    ExternalToolTIPSource,
    ObservedTIPRecord,
    register_external_tool_source,
)
from tokenpak.vault.sources.claude_transcript import (
    default_projects_root,
    iter_session_files,
)

logger = logging.getLogger(__name__)

TOOL_SLUG = "gstack"
ENV_STATE_ROOT = "GSTACK_STATE_ROOT"

#: Slash-command invocation: ``/gstack:<subcommand>`` or bare ``/gstack``.
#: The subcommand token is captured dynamically — no command/phase enum.
#: First alternative grabs a well-formed token; the ``\S+`` fallback grabs
#: malformed shapes so they can be *explicitly* skipped + logged downstream.
_INVOCATION_RE = re.compile(
    r"(?:^|[\s>\"'`(])/gstack(?::([A-Za-z0-9._-]+|\S+)|(?![A-Za-z0-9._-]))",
    re.MULTILINE,
)

#: Inline ``role=<x>`` / ``phase: <x>`` hints near an invocation.
_HINT_RE = re.compile(
    r"\b(role|phase)\s*[:=]\s*([A-Za-z0-9][A-Za-z0-9._-]*)",
    re.IGNORECASE,
)

#: Sanitized label-token charset (segment of an ``ext.*`` capability label).
_LABEL_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def state_root(env: Optional[Dict[str, str]] = None) -> Optional[Path]:
    """Optional gstack state dir for enrichment — env-configured, never assumed."""
    source = env if env is not None else os.environ
    raw = (source.get(ENV_STATE_ROOT) or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _sanitize_token(raw: str) -> Optional[str]:
    """Normalize a detected token into label charset; ``None`` if unusable.

    Deliberately strict: a token whose lowercase form does not already
    conform is treated as an *unknown shape* (skip + log at the call
    site) rather than mangled into a valid-looking label.
    """
    token = raw.strip().lower()
    if not token or not _LABEL_TOKEN_RE.match(token):
        return None
    return token


def _text_of(content: Any) -> str:
    """Tolerant text extraction from a transcript ``message.content`` value."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                txt = item.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
        return "\n".join(parts)
    return ""


def _usage_of(obj: Dict[str, Any]) -> Dict[str, int]:
    """Version-tolerant token-counter extraction from an assistant event.

    Sums any integer ``*_tokens`` counters found under ``message.usage``
    — no pin to a particular transcript schema version.
    """
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return {}
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return {}
    out: Dict[str, int] = {}
    for key, val in usage.items():
        if isinstance(key, str) and key.endswith("_tokens") and isinstance(val, int):
            out[key] = val
    return out


# ---------------------------------------------------------------------------
# Session event parsing (read-only)
# ---------------------------------------------------------------------------

def parse_session_events(path: Path) -> List[Dict[str, Any]]:
    """Parse a transcript JSONL into minimal event dicts, tolerantly.

    Unknown record kinds and corrupt JSON lines are skipped (with a
    DEBUG note) — never fatal (packet AC #1).
    """
    events: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug(
                        "gstack_tip: skip source=%s line=%d reason=corrupt-json",
                        path, lineno,
                    )
                    continue
                if not isinstance(obj, dict):
                    continue
                kind = obj.get("type")
                if kind == "user":
                    msg = obj.get("message")
                    text = _text_of(msg.get("content")) if isinstance(msg, dict) else ""
                    events.append({
                        "kind": "user",
                        "text": text,
                        "timestamp": obj.get("timestamp"),
                    })
                elif kind == "assistant":
                    events.append({
                        "kind": "assistant",
                        "usage": _usage_of(obj),
                        "timestamp": obj.get("timestamp"),
                    })
                # Other kinds (system, file-history-snapshot, future shapes)
                # carry no gstack signal today — tolerated, not an error.
    except OSError as exc:
        logger.warning("gstack_tip: skip source=%s reason=unreadable error=%s",
                       path, exc)
        return []
    return events


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

@register_external_tool_source
class GstackTIPSource(ExternalToolTIPSource):
    """Read-only gstack → TIP observation adapter (Std 23 §9 category)."""

    tool_slug = TOOL_SLUG

    static_capabilities = frozenset({
        f"ext.{TOOL_SLUG}.usage_observed",
        f"ext.{TOOL_SLUG}.cost_observed",
    })

    def __init__(
        self,
        projects_root: Optional[Path] = None,
        gstack_state_root: Optional[Path] = None,
    ) -> None:
        self._projects_root = projects_root
        self._state_root = (
            gstack_state_root if gstack_state_root is not None else state_root()
        )

    # -- collection --------------------------------------------------------

    def collect(self) -> List[ObservedTIPRecord]:
        """Walk discovered transcripts on demand and emit observed records."""
        root = self._projects_root or default_projects_root()
        hints = self._read_state_hints()
        records: List[ObservedTIPRecord] = []
        for project_dir, jsonl in iter_session_files(root):
            events = parse_session_events(jsonl)
            if not events:
                continue
            records.extend(
                self.detect(
                    events,
                    session_id=jsonl.stem,
                    source_path=str(jsonl),
                    state_hints=hints,
                )
            )
        return records

    def detect(
        self,
        events: Iterable[Dict[str, Any]],
        *,
        session_id: Optional[str] = None,
        source_path: Optional[str] = None,
        state_hints: Optional[Dict[str, str]] = None,
    ) -> List[ObservedTIPRecord]:
        """Map parsed session events → observed TIP records.

        A span opens at each detected gstack invocation and closes at
        the next invocation (or end of session).  Assistant token
        counters within the span are summed as the companion-observed
        usage for that invocation.
        """
        records: List[ObservedTIPRecord] = []
        span: Optional[Dict[str, Any]] = None

        def close_span() -> None:
            nonlocal span
            if span is None:
                return
            record = self._build_record(
                span, session_id=session_id, source_path=source_path,
                state_hints=state_hints or {},
            )
            if record is not None:
                records.append(record)
            span = None

        for event in events:
            if event.get("kind") == "user":
                text = event.get("text") or ""
                match = _INVOCATION_RE.search(text)
                if match:
                    close_span()
                    span = {
                        "raw_command": match.group(1),
                        "text": text,
                        "first_timestamp": event.get("timestamp"),
                        "last_timestamp": event.get("timestamp"),
                        "usage": {},
                        "messages": 0,
                    }
            elif event.get("kind") == "assistant" and span is not None:
                span["messages"] += 1
                if event.get("timestamp"):
                    span["last_timestamp"] = event["timestamp"]
                for key, val in (event.get("usage") or {}).items():
                    span["usage"][key] = span["usage"].get(key, 0) + val
        close_span()
        return records

    # -- record assembly ----------------------------------------------------

    def _build_record(
        self,
        span: Dict[str, Any],
        *,
        session_id: Optional[str],
        source_path: Optional[str],
        state_hints: Dict[str, str],
    ) -> Optional[ObservedTIPRecord]:
        labels = {f"ext.{TOOL_SLUG}.usage_observed"}

        command: Optional[str] = None
        raw_command = span.get("raw_command")
        if raw_command:
            command = _sanitize_token(raw_command)
            if command is None:
                logger.info(
                    "gstack_tip: skip session=%s reason=unrecognized-command-shape "
                    "raw=%r", session_id, raw_command,
                )
                return None
            labels.add(f"ext.{TOOL_SLUG}.command.{command}")

        # Inline role/phase hints in the invoking message (dynamic — no enum),
        # falling back to optional GSTACK_STATE_ROOT enrichment.
        role: Optional[str] = None
        phase: Optional[str] = None
        for key, raw in _HINT_RE.findall(span.get("text") or ""):
            token = _sanitize_token(raw)
            if token is None:
                logger.info(
                    "gstack_tip: skip-hint session=%s reason=unrecognized-%s-shape "
                    "raw=%r", session_id, key.lower(), raw,
                )
                continue
            if key.lower() == "role" and role is None:
                role = token
            elif key.lower() == "phase" and phase is None:
                phase = token
        role = role or state_hints.get("role")
        phase = phase or state_hints.get("phase")

        if role:
            labels.add(f"ext.{TOOL_SLUG}.role.{role}")
        if phase:
            labels.add(f"ext.{TOOL_SLUG}.phase.{phase}")

        usage: Dict[str, int] = dict(span.get("usage") or {})
        if usage:
            labels.add(f"ext.{TOOL_SLUG}.cost_observed")
        usage["assistant_messages"] = int(span.get("messages") or 0)

        return ObservedTIPRecord(
            tool=TOOL_SLUG,
            labels=sorted(labels),
            command=command,
            role=role,
            phase=phase,
            session_id=session_id,
            source_path=source_path,
            first_timestamp=span.get("first_timestamp"),
            last_timestamp=span.get("last_timestamp"),
            observed_usage=usage,
        )

    # -- optional read-only enrichment ---------------------------------------

    def _read_state_hints(self) -> Dict[str, str]:
        """Best-effort role/phase hints from ``GSTACK_STATE_ROOT`` JSON files.

        Read-only and fully optional: missing dir, unreadable files, or
        unknown shapes all degrade to "no hints" with a log line.
        """
        root = self._state_root
        if root is None or not root.is_dir():
            return {}
        hints: Dict[str, str] = {}
        try:
            candidates = sorted(root.glob("*.json"))
        except OSError as exc:
            logger.info("gstack_tip: skip-state root=%s reason=unreadable error=%s",
                        root, exc)
            return {}
        for path in candidates:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.info(
                    "gstack_tip: skip-state file=%s reason=unparseable error=%s",
                    path, exc,
                )
                continue
            for scope in self._hint_scopes(data):
                for key in ("role", "phase"):
                    if key in hints:
                        continue
                    val = scope.get(key)
                    token = _sanitize_token(val) if isinstance(val, str) else None
                    if token:
                        hints[key] = token
        return hints

    @staticmethod
    def _hint_scopes(data: Any) -> Tuple[Dict[str, Any], ...]:
        """Yield dict scopes that may carry role/phase keys, version-tolerantly."""
        if not isinstance(data, dict):
            return ()
        scopes: List[Dict[str, Any]] = [data]
        current = data.get("current")
        if isinstance(current, dict):
            scopes.append(current)
        return tuple(scopes)


__all__ = [
    "TOOL_SLUG",
    "ENV_STATE_ROOT",
    "GstackTIPSource",
    "parse_session_events",
    "state_root",
]
