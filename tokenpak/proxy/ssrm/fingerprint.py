"""SSRM fingerprint — canonical-prompt hashing + recent-fingerprint memory.

Phase 1 canonical form: the LAST user-turn body, with whitespace
collapsed and ISO-date timestamps replaced by a constant. This catches
"same prompt, slightly different framing" repeats — the multi-cycle
no-progress pattern observed in the 2026-05-13 incident — without
false-positive matching legitimate multi-step work.

We deliberately do NOT include the system prompt prefix in the canonical
form. The same agent calling a different tool or moving to a different
task should produce a different fingerprint even with the same system
prompt. Future tightening is open-decision 3 in the parent packet.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Iterable, Optional

from .state import open_state_db


# Collapse runs of whitespace to single spaces.
_WS_RE = re.compile(r"\s+")
# Replace ISO-8601 dates / datetimes with a stable placeholder so the same
# prompt issued on different days hashes identically.
_DATE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)
# Replace epoch seconds-or-ms longer-than-9-digits with a placeholder.
_EPOCH_RE = re.compile(r"\b\d{10,13}\b")


def canonicalize_user_turn(body_obj: dict) -> str:
    """Return the canonicalized last-user-turn text for fingerprinting.

    Returns '' if no user turn is present.
    """
    if not isinstance(body_obj, dict):
        return ""
    messages = body_obj.get("messages") or []
    if not isinstance(messages, list):
        return ""
    last_user_text = ""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            last_user_text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for c in content:
                if isinstance(c, dict):
                    if c.get("type") == "text" and isinstance(c.get("text"), str):
                        parts.append(c["text"])
                elif isinstance(c, str):
                    parts.append(c)
            if parts:
                last_user_text = "\n".join(parts)
    if not last_user_text:
        return ""
    s = _DATE_RE.sub("<DATE>", last_user_text)
    s = _EPOCH_RE.sub("<EPOCH>", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def hash_canonical(canonical: str) -> str:
    """Return a stable, short hex hash for a canonicalized string."""
    if not canonical:
        return ""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def fingerprint_of_body(body: bytes | str | dict) -> str:
    """Compute the fingerprint hash for a request body.

    Accepts raw bytes, decoded string, or already-parsed dict.
    Returns '' for unparseable bodies.
    """
    if isinstance(body, bytes):
        try:
            body_obj = json.loads(body.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return ""
    elif isinstance(body, str):
        try:
            body_obj = json.loads(body)
        except json.JSONDecodeError:
            return ""
    elif isinstance(body, dict):
        body_obj = body
    else:
        return ""
    return hash_canonical(canonicalize_user_turn(body_obj))


def record_and_count(
    session_id: str,
    prompt_hash: str,
    state_db_path: str,
    *,
    now: Optional[float] = None,
    ttl_seconds: int = 7200,
) -> int:
    """Record a fingerprint observation and return the repeat count.

    Returns the *post-write* seen_count for (session_id, prompt_hash).
    Returns 0 if either arg is empty.

    Side effect: prunes rows older than `ttl_seconds` for this session
    before recording the new observation.
    """
    if not session_id or not prompt_hash:
        return 0
    now = float(now) if now is not None else time.time()
    conn = open_state_db(state_db_path)
    cutoff = now - ttl_seconds
    conn.execute(
        "DELETE FROM fingerprints WHERE session_id = ? AND last_seen < ?",
        (session_id, cutoff),
    )
    existing = conn.execute(
        "SELECT seen_count FROM fingerprints WHERE session_id = ? AND prompt_hash = ?",
        (session_id, prompt_hash),
    ).fetchone()
    if existing is None:
        conn.execute(
            """INSERT INTO fingerprints
               (session_id, prompt_hash, first_seen, last_seen, seen_count)
               VALUES (?, ?, ?, ?, 1)""",
            (session_id, prompt_hash, now, now),
        )
        new_count = 1
    else:
        new_count = int(existing[0]) + 1
        conn.execute(
            """UPDATE fingerprints
               SET last_seen = ?, seen_count = ?
               WHERE session_id = ? AND prompt_hash = ?""",
            (now, new_count, session_id, prompt_hash),
        )
    conn.commit()
    return new_count
