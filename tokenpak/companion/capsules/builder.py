"""
tokenpak.companion.capsules.builder
===================================

Capsule Builder — deterministic companion memory-capsule generation and
context-block compression helpers.

A **capsule** is a compact, structured representation of verbose prior-session
context. The session builder consumes normalized messages from a
``TranscriptSource`` so platform-specific transcript discovery and parsing stay
outside this module. The request-body ``process`` API is preserved for existing
proxy integration callers.

Design Principles
-----------------
* **Deterministic** — SHA-256 of normalized content drives stable IDs.
* **Fast** — pure string operations only; no model calls.
* **Transparent** — capsule output is readable plain text.
* **Safe** — missing transcript sources return ``None`` so callers can fall
  back to vault retrieval only.
* **Feature-flag-compatible** — request-body compression remains disabled by
  default unless enabled by the caller.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from tokenpak.companion.capsules.transcript_sources.base import Message, TranscriptSource

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum character length of a text block before the builder considers
# compressing it.  Below this threshold the overhead of a capsule envelope
# would exceed the savings.
DEFAULT_MIN_BLOCK_CHARS: int = 400

# Number of most-recent messages to leave untouched (the "hot window").
# Capsule compression is only applied to messages *outside* this window.
DEFAULT_HOT_WINDOW: int = 2

# Minimum normalized message count before a memory capsule is useful.
DEFAULT_MIN_MESSAGE_COUNT: int = 5

# Maximum chars to keep per paragraph in compressed form.
_MAX_PARA_CHARS: int = 200

# Pre-compiled patterns (module-level for reuse across calls)
_RE_MULTI_BLANK = re.compile(r"\n{3,}")
_RE_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")
_RE_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_RE_ARTIFACT = re.compile(r"(?P<path>[\w./-]+\.(?:py|md|json|yaml|yml|toml|txt|sh))")
_RE_ACTION = re.compile(r"\b(todo|follow[- ]?up|next|fix|review|verify|run|add)\b", re.I)
_RE_DECISION = re.compile(r"\b(decid(?:e|ed|es|ing)|choose|chose|use|prefer|ship|keep)\b", re.I)
_RE_INSIGHT = re.compile(r"\b(because|root cause|lesson|gotcha|insight|found|discovered)\b", re.I)


@dataclass(frozen=True)
class SessionCapsule:
    """Deterministic companion memory capsule for one session."""

    session_id: str
    source_name: str
    message_count: int
    content: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capsule_id(content: str) -> str:
    """Return a short deterministic ID for *content* (8 hex chars)."""
    digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    return digest[:8]


def _compress_paragraph(para: str) -> str:
    """
    Compress a single prose paragraph deterministically.

    Strategy (in order):
    1. Collapse internal whitespace.
    2. If the paragraph ends with a sentence boundary within ``_MAX_PARA_CHARS``,
       truncate there.
    3. Hard-truncate at ``_MAX_PARA_CHARS`` on a word boundary.
    """
    # Collapse runs of spaces / tabs (not newlines — those separate paragraphs)
    text = _RE_MULTI_SPACE.sub(" ", para).strip()

    if len(text) <= _MAX_PARA_CHARS:
        return text

    # Try to find a sentence end within budget
    m = None
    for m in _RE_SENTENCE_END.finditer(text):
        if m.end() > _MAX_PARA_CHARS:
            break
    if m and m.end() <= _MAX_PARA_CHARS:
        return text[: m.end()].strip()

    # Fall back to word-boundary truncation
    truncated = text[:_MAX_PARA_CHARS]
    last_space = truncated.rfind(" ")
    if last_space > _MAX_PARA_CHARS // 2:
        truncated = truncated[:last_space]
    return truncated.rstrip() + "…"


def _compress_text(text: str) -> str:
    """
    Compress *text* by applying paragraph-level compression.

    Structure-bearing lines (headers ``#``, bullets ``- / * / +``, numbered
    lists ``1.``, code fences ```` ``` ````) are preserved verbatim.
    Prose paragraphs are compressed.

    Returns the compressed text.  Always deterministic.
    """
    # Normalise excessive blank lines first
    text = _RE_MULTI_BLANK.sub("\n\n", text).strip()

    # Split into logical blocks separated by blank lines
    blocks = re.split(r"\n{2,}", text)
    compressed_blocks: list[str] = []

    in_code_fence = False

    for block in blocks:
        lines = block.split("\n")
        out_lines: list[str] = []

        for line in lines:
            stripped = line.strip()

            # Track code fences — never compress inside them
            if stripped.startswith("```"):
                in_code_fence = not in_code_fence
                out_lines.append(line)
                continue

            if in_code_fence:
                out_lines.append(line)
                continue

            # Structure lines — keep verbatim
            if (
                stripped.startswith("#")  # heading
                or re.match(r"^[-*+]\s", stripped)  # unordered bullet
                or re.match(r"^\d+\.\s", stripped)  # ordered list
                or stripped.startswith(">")  # blockquote
                or stripped == "---"
                or stripped == "==="  # hr / setext heading
                or stripped == ""  # blank line within block
            ):
                out_lines.append(line)
                continue

            # Prose line — compress
            out_lines.append(_compress_paragraph(stripped))

        compressed_blocks.append("\n".join(out_lines))

    return "\n\n".join(compressed_blocks)


def load_capsule(path: str) -> str:
    """Read and return the contents of a capsule file at *path*.

    Returns an empty string if the file cannot be read.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def save_capsule(capsule: SessionCapsule, *, capsule_dir: Path | str) -> Path:
    """Persist ``capsule`` as ``<session_id>.md`` and return its path."""
    output_dir = Path(capsule_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_session_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", capsule.session_id).strip("-._")
    if not safe_session_id:
        safe_session_id = _capsule_id(capsule.session_id or capsule.content)
    output_path = output_dir / f"{safe_session_id}.md"
    output_path.write_text(capsule.content, encoding="utf-8")
    return output_path


def _wrap_capsule(original: str, compressed: str) -> str:
    """
    Wrap *compressed* content in a capsule envelope.

    The capsule ID is derived from *original* (pre-compression) content so
    that the ID is stable even if the compressor changes.
    """
    cid = _capsule_id(original)
    chars_in = len(original)
    chars_out = len(compressed)
    ratio = round(chars_out / chars_in, 3) if chars_in else 1.0
    header = f"[CAPSULE id={cid} ratio={ratio} chars_in={chars_in} chars_out={chars_out}]"
    return f"{header}\n{compressed}\n[/CAPSULE]"


def _first_nonempty(messages: list[Message]) -> str:
    for message in messages:
        text = _one_line(message.content)
        if text:
            return text
    return "No context extracted."


def _one_line(text: str, *, limit: int = 220) -> str:
    value = re.sub(r"\s+", " ", text).strip()
    if len(value) <= limit:
        return value
    truncated = value[:limit]
    last_space = truncated.rfind(" ")
    if last_space > limit // 2:
        truncated = truncated[:last_space]
    return truncated.rstrip() + "…"


def _unique_append(items: list[str], value: str, *, limit: int) -> None:
    cleaned = _one_line(value)
    if cleaned and cleaned not in items and len(items) < limit:
        items.append(cleaned)


def _message_content_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    if isinstance(value, Mapping):
        text = value.get("text") or value.get("content")
        if isinstance(text, str):
            return text
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


# ---------------------------------------------------------------------------
# CapsuleBuilder
# ---------------------------------------------------------------------------


class CapsuleBuilder:
    """
    Build memory capsules and compress verbose request context blocks.

    Parameters
    ----------
    transcript_source : TranscriptSource | None
        Optional source for prior-session messages. If omitted, :meth:`build`
        returns ``None`` and callers can fall back to retrieval-only behavior.
    min_message_count : int
        Minimum source message count required for memory-capsule generation.
    enabled : bool
        Master switch for the request-body :meth:`process` API. When *False*
        (the default), :meth:`process` is a no-op.
    min_block_chars : int
        Minimum character length of a text block to qualify for request-body
        compression.
    hot_window : int
        Number of trailing messages to leave untouched by request-body
        compression.
    """

    def __init__(
        self,
        transcript_source: TranscriptSource | None = None,
        *,
        min_message_count: int = DEFAULT_MIN_MESSAGE_COUNT,
        enabled: bool = False,
        min_block_chars: int = DEFAULT_MIN_BLOCK_CHARS,
        hot_window: int = DEFAULT_HOT_WINDOW,
    ) -> None:
        self._source = transcript_source
        self._min_message_count = min_message_count
        self._enabled = enabled
        self._min_block_chars = min_block_chars
        self._hot_window = hot_window

    # ------------------------------------------------------------------
    # Public API — memory capsules
    # ------------------------------------------------------------------

    def build(self, session_id: str) -> Optional[SessionCapsule]:
        """Build a memory capsule for ``session_id`` or return ``None``.

        ``None`` is the graceful-degrade signal for surfaces that cannot expose
        prior-session transcripts. Cache injection callers should treat it as
        "vault retrieval only" rather than as an error.
        """
        if self._source is None:
            return None

        messages = list(self._source.load_messages(session_id))
        return self.build_from_messages(
            messages,
            session_id=session_id,
            source_name=self._source.source_name,
        )

    def build_from_messages(
        self,
        messages: Iterable[Message | Mapping[str, Any]],
        *,
        session_id: str = "session",
        source_name: str = "manual",
    ) -> Optional[SessionCapsule]:
        """Build a memory capsule from already-loaded normalized messages.

        This is the scheduler-friendly API used by daily companion capsule
        generation. Platform-specific transcript discovery stays outside the
        builder; callers may pass ``Message`` objects or dict-like records with
        ``role``/``type``, ``content``, ``timestamp``, and optional ``metadata``.
        """
        normalized = [self._coerce_message(message) for message in messages]
        normalized = [message for message in normalized if message.content.strip()]
        if len(normalized) < self._min_message_count:
            return None
        return self._summarize(session_id, normalized, source_name=source_name)

    @staticmethod
    def _coerce_message(message: Message | Mapping[str, Any]) -> Message:
        if isinstance(message, Message):
            return message

        content = _message_content_to_text(message.get("content", ""))
        role = str(message.get("role") or message.get("type") or "")
        timestamp = str(message.get("timestamp") or "")
        raw_metadata = message.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
        return Message(role=role, content=content, timestamp=timestamp, metadata=metadata)

    # ------------------------------------------------------------------
    # Public API — request-body compression (existing behavior)
    # ------------------------------------------------------------------

    def process(
        self,
        body_bytes: bytes,
    ) -> Tuple[bytes, Dict[str, Any]]:
        """
        Process the request body, capsulising eligible context blocks.

        Parameters
        ----------
        body_bytes : bytes
            Raw JSON request body (OpenAI / Anthropic chat format).

        Returns
        -------
        (new_body_bytes, stats)
            *new_body_bytes* — modified body (or original if nothing changed).
            *stats* — dict with keys:
                ``blocks_capsulized`` int,
                ``chars_in``         int,
                ``chars_out``        int,
                ``ratio``            float,
                ``duration_ms``      float.
        """
        _empty_stats: Dict[str, Any] = {
            "blocks_capsulized": 0,
            "chars_in": 0,
            "chars_out": 0,
            "ratio": 1.0,
            "duration_ms": 0.0,
            "skipped": True,
            "skip_reason": "disabled",
        }

        if not self._enabled:
            return body_bytes, _empty_stats

        t0 = time.monotonic()
        try:
            data = json.loads(body_bytes)
        except (json.JSONDecodeError, ValueError):
            return body_bytes, {**_empty_stats, "skip_reason": "invalid_json"}

        messages: list[dict[str, Any]] = data.get("messages") or []
        if not messages:
            stats = {**_empty_stats, "skip_reason": "no_messages", "duration_ms": 0.0}
            return body_bytes, stats

        # Determine the hot window: last `hot_window` messages are untouched
        hot_start = max(0, len(messages) - self._hot_window)

        total_chars_in = 0
        total_chars_out = 0
        blocks_capsulized = 0
        modified = False

        for idx, msg in enumerate(messages):
            if idx >= hot_start:
                # Inside hot window — never touch
                continue
            if not isinstance(msg, dict):
                continue

            content = msg.get("content")

            if isinstance(content, str):
                new_content, delta_in, delta_out, capsulized = self._maybe_capsulise(content)
                if capsulized:
                    msg["content"] = new_content
                    modified = True
                total_chars_in += delta_in
                total_chars_out += delta_out
                blocks_capsulized += capsulized

            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        new_text, delta_in, delta_out, capsulized = self._maybe_capsulise(
                            part["text"]
                        )
                        if capsulized:
                            part["text"] = new_text
                            modified = True
                        total_chars_in += delta_in
                        total_chars_out += delta_out
                        blocks_capsulized += capsulized

        duration_ms = (time.monotonic() - t0) * 1000

        if not modified:
            ratio = 1.0
            stats: Dict[str, Any] = {  # type: ignore[no-redef]
                "blocks_capsulized": 0,
                "chars_in": total_chars_in,
                "chars_out": total_chars_in,
                "ratio": ratio,
                "duration_ms": round(duration_ms, 3),
                "skipped": False,
                "skip_reason": "no_eligible_blocks",
            }
            return body_bytes, stats

        new_body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        ratio = round(total_chars_out / total_chars_in, 3) if total_chars_in else 1.0

        stats = {
            "blocks_capsulized": blocks_capsulized,
            "chars_in": total_chars_in,
            "chars_out": total_chars_out,
            "ratio": ratio,
            "duration_ms": round(duration_ms, 3),
            "skipped": False,
            "skip_reason": None,
        }
        return new_body, stats

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _summarize(
        self,
        session_id: str,
        messages: list[Message],
        *,
        source_name: str,
    ) -> SessionCapsule:
        context = _first_nonempty(messages)
        decisions: list[str] = []
        artifacts: list[str] = []
        actions: list[str] = []
        insights: list[str] = []

        for message in messages:
            for line in message.content.splitlines():
                stripped = line.strip(" -\t")
                if not stripped:
                    continue
                if _RE_DECISION.search(stripped):
                    _unique_append(decisions, stripped, limit=8)
                if _RE_ACTION.search(stripped):
                    _unique_append(actions, stripped, limit=8)
                if _RE_INSIGHT.search(stripped):
                    _unique_append(insights, stripped, limit=8)
                for match in _RE_ARTIFACT.finditer(stripped):
                    _unique_append(artifacts, match.group("path"), limit=12)

        if not insights:
            _unique_append(insights, f"Built from {len(messages)} normalized messages.", limit=8)

        raw_text = "\n".join(message.content for message in messages)
        metadata = {
            "sha256": hashlib.sha256(raw_text.encode("utf-8", errors="replace")).hexdigest(),
            "source_name": source_name,
        }
        content = self._render_capsule(
            session_id=session_id,
            source_name=source_name,
            message_count=len(messages),
            metadata=metadata,
            context=context,
            decisions=decisions,
            artifacts=artifacts,
            actions=actions,
            insights=insights,
        )
        return SessionCapsule(
            session_id=session_id,
            source_name=source_name,
            message_count=len(messages),
            content=content,
            metadata=metadata,
        )

    def _render_capsule(
        self,
        *,
        session_id: str,
        source_name: str,
        message_count: int,
        metadata: Mapping[str, Any],
        context: str,
        decisions: list[str],
        artifacts: list[str],
        actions: list[str],
        insights: list[str],
    ) -> str:
        sections = [
            "---",
            f"session_id: {session_id}",
            f"source_name: {source_name}",
            f"message_count: {message_count}",
            f"sha256: {metadata.get('sha256', '')}",
            "---",
            "",
            "# Context",
            context,
            "",
            "# Decisions",
            *_bullet_lines(decisions),
            "",
            "# Artifacts",
            *_bullet_lines(artifacts),
            "",
            "# Action items",
            *_bullet_lines(actions),
            "",
            "# Insights",
            *_bullet_lines(insights),
            "",
            "# Raw transcript reference",
            f"- source_name: {source_name}",
            f"- message_count: {message_count}",
            f"- sha256: {metadata.get('sha256', '')}",
            "",
        ]
        return "\n".join(sections)

    def _maybe_capsulise(self, text: str) -> Tuple[str, int, int, int]:
        """
        Conditionally capsulise a single text block.

        Returns
        -------
        (new_text, chars_in, chars_out, capsulized)
            *capsulized* is 1 if the block was wrapped, 0 otherwise.
        """
        chars_in = len(text)

        if chars_in < self._min_block_chars:
            return text, chars_in, chars_in, 0

        compressed = _compress_text(text)
        wrapped = _wrap_capsule(text, compressed)
        return wrapped, chars_in, len(wrapped), 1


def _bullet_lines(values: list[str]) -> list[str]:
    if not values:
        return ["- None captured"]
    return [f"- {value}" for value in values]
