# SPDX-License-Identifier: Apache-2.0
"""Upstream retry recovery record format and persistence.

Records are written to <tokenpak-home>/recovery/upstream/ as JSON files.
Credential headers are always redacted.  Full request bodies are never
persisted unless TOKENPAK_RETRY_PERSIST_BODY=1 is set in the environment.

Record lifecycle
----------------
- ``write_record()`` — called by the proxy when an upstream request fails
  terminally after all retry escalation is exhausted.
- ``list_record_files()`` — enumerate (path, record) pairs; oldest first.
- ``most_recent_failed()`` — return the newest record for ``codex continue``.
- ``delete_record_file()`` — called by ``retry drain`` after processing.

Deterministic failures (4xx other than 429) are recorded but never retried
by ``drain``.  Visible-continuation records are skipped by ``drain`` unless
the caller opts into visible-turn mode explicitly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Credential-header redaction ────────────────────────────────────────────

_CREDENTIAL_PATTERNS = [
    re.compile(r"^x-api-key$", re.IGNORECASE),
    re.compile(r"^authorization$", re.IGNORECASE),
    re.compile(r"^proxy-authorization$", re.IGNORECASE),
    re.compile(r"^cookie$", re.IGNORECASE),
    re.compile(r"^x-auth-token$", re.IGNORECASE),
    re.compile(r"^x-forwarded-authorization$", re.IGNORECASE),
    re.compile(r"^x-session-token$", re.IGNORECASE),
]
_REDACTED = "[REDACTED]"

_BODY_PREVIEW_BYTES = 200


def redact_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """Return a copy of *headers* with credential values replaced by [REDACTED]."""
    out: Dict[str, str] = {}
    for k, v in headers.items():
        out[k] = _REDACTED if any(p.match(k) for p in _CREDENTIAL_PATTERNS) else v
    return out


# ── Record schema ──────────────────────────────────────────────────────────

# Terminal recovery status values.
STATUS_TERMINAL = "terminal"
STATUS_RETRYABLE = "retryable"
STATUS_DETERMINISTIC = "deterministic_failure"


@dataclass
class UpstreamRetryRecord:
    """Redacted metadata for a failed upstream request.

    Full request bodies are only present when body_persisted is True
    (set via TOKENPAK_RETRY_PERSIST_BODY=1).  Never serialize body_full
    in user-visible output; use body_preview / body_hash instead.
    """

    request_id: str
    tip_plan_id: Optional[str]
    # Target metadata
    endpoint: str
    provider: Optional[str]
    model: Optional[str]
    # Headers with credentials redacted
    headers_redacted: Dict[str, str]
    # Body material — safe to surface to users
    body_hash: Optional[str]          # sha256:<hex>
    body_preview: Optional[str]       # first 200 bytes decoded lossy
    body_persisted: bool              # True only if TOKENPAK_RETRY_PERSIST_BODY=1
    body_full: Optional[str]          # only non-None when body_persisted=True
    # State
    stream_started: bool
    terminal_recovery_status: str     # STATUS_* constant
    visible_continuation_required: bool
    # Timestamps
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    schema_version: int = 1

    def is_deterministic_failure(self) -> bool:
        return self.terminal_recovery_status == STATUS_DETERMINISTIC

    def safe_dict(self) -> Dict[str, Any]:
        """Serialize to dict without body_full."""
        d = asdict(self)
        d["body_full"] = None
        return d


# ── Storage helpers ────────────────────────────────────────────────────────

def _recovery_dir() -> Path:
    try:
        from tokenpak import _paths
        base = _paths.home()
    except Exception:
        base = Path.home() / ".tokenpak"
    d = base / "recovery" / "upstream"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _record_filename(record: UpstreamRetryRecord) -> str:
    ts = record.created_at[:19].replace(":", "-")  # 2026-06-20T07-02-11
    return f"{ts}_{record.request_id[:16]}.json"


# ── Write ──────────────────────────────────────────────────────────────────

def write_record(
    *,
    request_id: str,
    tip_plan_id: Optional[str] = None,
    endpoint: str,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    stream_started: bool = False,
    terminal_recovery_status: str = STATUS_TERMINAL,
    visible_continuation_required: bool = True,
) -> UpstreamRetryRecord:
    """Write a redacted retry record to the recovery directory.

    Returns the in-memory record; does not include body_full in the on-disk
    JSON unless TOKENPAK_RETRY_PERSIST_BODY=1.
    """
    persist_body = os.environ.get("TOKENPAK_RETRY_PERSIST_BODY", "0").strip() == "1"
    body_hash: Optional[str] = None
    body_preview: Optional[str] = None
    body_full: Optional[str] = None

    if body:
        body_hash = "sha256:" + hashlib.sha256(body).hexdigest()
        body_preview = body[:_BODY_PREVIEW_BYTES].decode("utf-8", errors="replace")
        if persist_body:
            body_full = body.decode("utf-8", errors="replace")

    record = UpstreamRetryRecord(
        request_id=request_id,
        tip_plan_id=tip_plan_id,
        endpoint=endpoint,
        provider=provider,
        model=model,
        headers_redacted=redact_headers(headers or {}),
        body_hash=body_hash,
        body_preview=body_preview,
        body_persisted=persist_body,
        body_full=body_full,
        stream_started=stream_started,
        terminal_recovery_status=terminal_recovery_status,
        visible_continuation_required=visible_continuation_required,
    )

    payload = asdict(record)
    if not persist_body:
        payload["body_full"] = None

    path = _recovery_dir() / _record_filename(record)
    path.write_text(json.dumps(payload, indent=2))
    return record


# ── Read / enumerate ───────────────────────────────────────────────────────

def _load_file(path: Path) -> Optional[UpstreamRetryRecord]:
    try:
        raw = json.loads(path.read_text())
        raw.pop("schema_version", None)
        return UpstreamRetryRecord(**raw)
    except Exception:
        return None


def list_record_files() -> List[Tuple[Path, UpstreamRetryRecord]]:
    """Return (path, record) pairs sorted oldest-first."""
    d = _recovery_dir()
    results: List[Tuple[Path, UpstreamRetryRecord]] = []
    for p in sorted(d.glob("*.json")):
        r = _load_file(p)
        if r is not None:
            results.append((p, r))
    return results


def most_recent_failed() -> Optional[Tuple[Path, UpstreamRetryRecord]]:
    """Return (path, record) for the newest record, or None."""
    items = list_record_files()
    return items[-1] if items else None


def delete_record_file(path: Path) -> bool:
    """Unlink a record file. Returns True if deleted."""
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


# ── Retry policy ───────────────────────────────────────────────────────────

_DEFAULT_MAX_RETRIES = int(os.environ.get("TOKENPAK_UPSTREAM_MAX_RETRIES", "3"))
_DEFAULT_RETRY_DELAY = float(os.environ.get("TOKENPAK_UPSTREAM_RETRY_DELAY", "1.0"))

# 4xx status codes that are deterministic failures (no retry)
_DETERMINISTIC_4XX = frozenset({400, 401, 403, 404, 422})


@dataclass
class UpstreamRetryPolicy:
    """Controls whether and how upstream requests are retried.

    Deterministic mode (TOKENPAK_DETERMINISTIC_MODE=1) disables all retries so
    test and eval runs produce stable, reproducible traces.

    Retries are never issued once any upstream output frame has been delivered
    to the caller (stream_started=True).
    """

    max_retries: int = _DEFAULT_MAX_RETRIES
    retry_delay_seconds: float = _DEFAULT_RETRY_DELAY
    deterministic: bool = False

    @classmethod
    def from_env(cls) -> "UpstreamRetryPolicy":
        deterministic = os.environ.get("TOKENPAK_DETERMINISTIC_MODE", "0").strip() == "1"
        return cls(
            max_retries=_DEFAULT_MAX_RETRIES,
            retry_delay_seconds=_DEFAULT_RETRY_DELAY,
            deterministic=deterministic,
        )

    def should_retry(self, *, status: int, attempt: int, stream_started: bool) -> bool:
        """Return True if the request should be retried.

        Args:
            status: HTTP status code from the upstream response.
            attempt: 0-based retry attempt number (0 = first try).
            stream_started: True if any output frame has already been sent.
        """
        if stream_started:
            return False
        if self.deterministic:
            return False
        if attempt >= self.max_retries:
            return False
        if status in _DETERMINISTIC_4XX:
            return False
        # 429 and 5xx are retryable
        return status == 429 or status >= 500

    def retry_after_seconds(self, headers: Dict[str, str]) -> float:
        """Return the delay in seconds before the next retry attempt.

        Uses the ``Retry-After`` header value (integer seconds) if present and
        valid; otherwise falls back to ``self.retry_delay_seconds``.
        """
        val = headers.get("Retry-After") or headers.get("retry-after")
        if val is not None:
            try:
                return max(0.0, float(val))
            except (ValueError, TypeError):
                pass
        return self.retry_delay_seconds

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


__all__ = [
    "UpstreamRetryRecord",
    "UpstreamRetryPolicy",
    "STATUS_TERMINAL",
    "STATUS_RETRYABLE",
    "STATUS_DETERMINISTIC",
    "redact_headers",
    "write_record",
    "list_record_files",
    "most_recent_failed",
    "delete_record_file",
]
