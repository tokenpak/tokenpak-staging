# SPDX-License-Identifier: Apache-2.0
"""Runtime Hygiene manifest schema, enums, and lifecycle validation.

Pure data + validation layer for the Runtime Hygiene registry foundation.
This module performs NO I/O — persistence and path resolution live in
:mod:`tokenpak.runtime.hygiene_registry`, and launch-time orchestration in
:mod:`tokenpak.runtime.hygiene`.

The manifest is the durable ownership record TokenPak writes before spawning
a session it may later be asked to clean up. The Unifying Rule of the
contract is that TokenPak may only ever clean sessions it *intentionally
launched, atomically registered, explicitly contained, freshly classified,
and can explain with machine-readable evidence*. This schema captures the
"intentionally launched + explicitly contained" evidence; classification and
cleanup are deliberately out of scope for the foundation packet.

Snapshot note: ``__all__ = []`` keeps this public-path module out of the
released-API snapshot (the registry foundation is internal plumbing, not a
public API claim). Callers import the names they need explicitly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

# Bumped only on a breaking manifest layout change. A reader that finds a
# manifest with a different schema_version must treat it as unusable rather
# than guessing — a partial/foreign manifest can never become cleanup
# authority (contract: "fail closed").
SCHEMA_VERSION = 1


# ── Enumerated field domains ─────────────────────────────────────────────
# Plain string constants (serialize straight to JSON) plus a frozenset of the
# allowed values for validation. Spelled as small namespace classes so call
# sites read ``Lifecycle.ACTIVE`` rather than a bare string literal.


class Lifecycle:
    """Persisted manifest lifecycle states (contract: "Lifecycle vs Classification")."""

    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"
    CLEANUP_ATTEMPTED = "cleanup_attempted"
    CLEANUP_FAILED = "cleanup_failed"
    RECEIPT_FAILED = "receipt_failed"


LIFECYCLE_STATES: frozenset[str] = frozenset(
    {
        Lifecycle.ACTIVE,
        Lifecycle.CLOSING,
        Lifecycle.CLOSED,
        Lifecycle.CLEANUP_ATTEMPTED,
        Lifecycle.CLEANUP_FAILED,
        Lifecycle.RECEIPT_FAILED,
    }
)


class ContainmentMethod:
    """How TokenPak contained the child, if at all (contract manifest fields)."""

    SYSTEMD_SCOPE = "systemd_scope"
    PGRP = "pgrp"
    CGROUP = "cgroup"
    JOB_OBJECT = "job_object"
    LAUNCHD = "launchd"
    NONE = "none"


CONTAINMENT_METHODS: frozenset[str] = frozenset(
    {
        ContainmentMethod.SYSTEMD_SCOPE,
        ContainmentMethod.PGRP,
        ContainmentMethod.CGROUP,
        ContainmentMethod.JOB_OBJECT,
        ContainmentMethod.LAUNCHD,
        ContainmentMethod.NONE,
    }
)


class LaunchMode:
    """Which TokenPak launch surface created the session."""

    CLAUDE = "claude"
    CODEX = "codex"
    COMPANION = "companion"
    PROXY = "proxy"
    OTHER = "other"


LAUNCH_MODES: frozenset[str] = frozenset(
    {
        LaunchMode.CLAUDE,
        LaunchMode.CODEX,
        LaunchMode.COMPANION,
        LaunchMode.PROXY,
        LaunchMode.OTHER,
    }
)


class CleanupPolicy:
    """What a future cleanup pass may do with this session.

    ``term_allowed`` is the only policy that can ever authorize a TERM, and
    it is only legitimate alongside TokenPak-created containment (enforced by
    :func:`tokenpak.runtime.hygiene.register_session`). ``never_touch`` is the
    fail-safe a non-cleanup launch downgrades to when its manifest could not
    be durably written.
    """

    REPORT_ONLY = "report_only"
    TERM_ALLOWED = "term_allowed"
    NEVER_TOUCH = "never_touch"


CLEANUP_POLICIES: frozenset[str] = frozenset(
    {
        CleanupPolicy.REPORT_ONLY,
        CleanupPolicy.TERM_ALLOWED,
        CleanupPolicy.NEVER_TOUCH,
    }
)


# ── Lifecycle transition table ───────────────────────────────────────────
# Contract "Lifecycle Transitions". Anything not listed fails closed; the
# terminal states (closed / cleanup_failed / receipt_failed) have no
# outgoing edges, so a closed session can never re-enter cleanup.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    Lifecycle.ACTIVE: frozenset({Lifecycle.CLOSING, Lifecycle.CLEANUP_ATTEMPTED}),
    Lifecycle.CLOSING: frozenset({Lifecycle.CLOSED}),
    Lifecycle.CLEANUP_ATTEMPTED: frozenset(
        {Lifecycle.CLOSED, Lifecycle.CLEANUP_FAILED, Lifecycle.RECEIPT_FAILED}
    ),
    Lifecycle.CLOSED: frozenset(),
    Lifecycle.CLEANUP_FAILED: frozenset(),
    Lifecycle.RECEIPT_FAILED: frozenset(),
}


# ── Error taxonomy ───────────────────────────────────────────────────────


class HygieneError(Exception):
    """Base class for all Runtime Hygiene foundation errors."""


class ManifestValidationError(HygieneError):
    """A manifest is missing required fields or carries an invalid enum value."""


class InvalidLifecycleTransition(HygieneError):
    """A requested lifecycle transition is not in :data:`ALLOWED_TRANSITIONS`."""


# ── Transition helpers ───────────────────────────────────────────────────


def is_valid_transition(old: str, new: str) -> bool:
    """Return True iff ``old -> new`` is an allowed lifecycle transition.

    Fails closed: an unknown ``old`` state has no outgoing edges, and a
    self-transition (``old == new``) is not allowed unless explicitly listed
    (none are).
    """
    return new in ALLOWED_TRANSITIONS.get(old, frozenset())


def assert_transition(old: str, new: str) -> None:
    """Raise :class:`InvalidLifecycleTransition` unless ``old -> new`` is allowed."""
    if old not in LIFECYCLE_STATES:
        raise InvalidLifecycleTransition(f"unknown source lifecycle state: {old!r}")
    if new not in LIFECYCLE_STATES:
        raise InvalidLifecycleTransition(f"unknown target lifecycle state: {new!r}")
    if not is_valid_transition(old, new):
        raise InvalidLifecycleTransition(
            f"lifecycle transition {old!r} -> {new!r} is not permitted; "
            f"allowed from {old!r}: {sorted(ALLOWED_TRANSITIONS.get(old, frozenset()))}"
        )


# ── Command-shape redaction ──────────────────────────────────────────────

# Cap the number of tokens recorded so a pathological argv cannot turn the
# manifest into a large process dump (contract: "redacted command shape, not
# full argv or env"; receipts must not include "large process dumps").
_MAX_SHAPE_TOKENS = 32


def redact_command_shape(command: "Sequence[str] | str | None") -> str:
    """Reduce an argv to a value-free shape string.

    Keeps the executable basename and the *names* of option flags, but
    replaces every flag value and positional argument with ``<arg>`` so no
    path, token, prompt, or other value can leak into the manifest.

    Examples
    --------
    ``["codex", "-p", "tokenpak-chatgpt", "--model=gpt-5"]``
        -> ``"codex -p <arg> --model"``
    ``"/usr/bin/claude --resume /tmp/secret"``
        -> ``"claude --resume <arg>"``
    """
    if command is None:
        return ""
    if isinstance(command, str):
        tokens = command.split()
    else:
        tokens = [str(t) for t in command]
    if not tokens:
        return ""

    shape: list[str] = [os.path.basename(tokens[0]) or tokens[0]]
    for tok in tokens[1:_MAX_SHAPE_TOKENS]:
        if tok.startswith("-"):
            # Keep the flag name only; drop any ``=value`` suffix.
            shape.append(tok.split("=", 1)[0])
        else:
            shape.append("<arg>")
    if len(tokens) > _MAX_SHAPE_TOKENS:
        shape.append("...")
    return " ".join(shape)


# ── Timestamp helper ─────────────────────────────────────────────────────


def utc_now_iso() -> str:
    """Return the current UTC time as a second-precision ISO-8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Manifest ─────────────────────────────────────────────────────────────


@dataclass
class SessionManifest:
    """Durable ownership + containment record for one TokenPak-launched session.

    Field set is the contract's "Minimum manifest requirements". Optional
    identity fields default to ``None`` because they are best-effort and
    platform-dependent (``boot_id``/``os_sid``/``process_group_id`` may be
    unavailable on macOS/Windows); their absence weakens later cleanup
    eligibility rather than blocking registration.
    """

    tokenpak_session_id: str
    pid: int
    uid: int
    state_home: str
    heartbeat_path: str
    launch_mode: str
    cleanup_policy: str
    containment_created_by_tokenpak: bool
    containment_method: str
    tokenpak_version: str
    launcher_version: str
    command_shape: str = ""
    schema_version: int = SCHEMA_VERSION
    lifecycle: str = Lifecycle.ACTIVE
    manifest_created_at: str = field(default_factory=utc_now_iso)
    lifecycle_updated_at: Optional[str] = None
    agent_id: Optional[str] = None
    pid_start_time: Optional[str] = None
    boot_id: Optional[str] = None
    process_group_id: Optional[int] = None
    os_sid: Optional[int] = None
    containment_id: Optional[str] = None

    def validate(self) -> "SessionManifest":
        """Validate enum domains + required fields; return self for chaining.

        Raises :class:`ManifestValidationError` on any violation. Validation
        is fail-closed: a manifest that does not validate must never be
        treated as cleanup authority by a downstream packet.
        """
        if self.schema_version != SCHEMA_VERSION:
            raise ManifestValidationError(
                f"unsupported schema_version {self.schema_version!r} "
                f"(this build understands {SCHEMA_VERSION})"
            )
        if not self.tokenpak_session_id:
            raise ManifestValidationError("tokenpak_session_id must be non-empty")
        if not isinstance(self.pid, int) or self.pid <= 0:
            raise ManifestValidationError(f"pid must be a positive int, got {self.pid!r}")
        if not isinstance(self.uid, int) or self.uid < 0:
            raise ManifestValidationError(f"uid must be a non-negative int, got {self.uid!r}")
        if not self.state_home:
            raise ManifestValidationError("state_home must be non-empty")
        if not self.heartbeat_path:
            raise ManifestValidationError("heartbeat_path must be non-empty")
        if self.launch_mode not in LAUNCH_MODES:
            raise ManifestValidationError(
                f"launch_mode {self.launch_mode!r} not in {sorted(LAUNCH_MODES)}"
            )
        if self.cleanup_policy not in CLEANUP_POLICIES:
            raise ManifestValidationError(
                f"cleanup_policy {self.cleanup_policy!r} not in {sorted(CLEANUP_POLICIES)}"
            )
        if self.containment_method not in CONTAINMENT_METHODS:
            raise ManifestValidationError(
                f"containment_method {self.containment_method!r} not in "
                f"{sorted(CONTAINMENT_METHODS)}"
            )
        if not isinstance(self.containment_created_by_tokenpak, bool):
            raise ManifestValidationError("containment_created_by_tokenpak must be a bool")
        if self.lifecycle not in LIFECYCLE_STATES:
            raise ManifestValidationError(
                f"lifecycle {self.lifecycle!r} not in {sorted(LIFECYCLE_STATES)}"
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain JSON-ready dict (stable key order)."""
        return {
            "schema_version": self.schema_version,
            "tokenpak_session_id": self.tokenpak_session_id,
            "manifest_created_at": self.manifest_created_at,
            "lifecycle": self.lifecycle,
            "lifecycle_updated_at": self.lifecycle_updated_at,
            "agent_id": self.agent_id,
            "tokenpak_version": self.tokenpak_version,
            "launcher_version": self.launcher_version,
            "pid": self.pid,
            "pid_start_time": self.pid_start_time,
            "uid": self.uid,
            "boot_id": self.boot_id,
            "process_group_id": self.process_group_id,
            "os_sid": self.os_sid,
            "containment_created_by_tokenpak": self.containment_created_by_tokenpak,
            "containment_method": self.containment_method,
            "containment_id": self.containment_id,
            "state_home": self.state_home,
            "heartbeat_path": self.heartbeat_path,
            "launch_mode": self.launch_mode,
            "cleanup_policy": self.cleanup_policy,
            "command_shape": self.command_shape,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionManifest":
        """Reconstruct a manifest from a parsed dict, dropping unknown keys.

        Does not validate — callers that need a trusted manifest call
        :meth:`validate` after. Missing keys raise :class:`ManifestValidationError`
        rather than silently defaulting, because a partial on-disk manifest is
        exactly the "fail closed" case the contract warns about.
        """
        if not isinstance(data, dict):
            raise ManifestValidationError(f"manifest must be an object, got {type(data).__name__}")
        known = {f for f in cls.__dataclass_fields__}  # noqa: F841 (clarity)
        required = {
            "tokenpak_session_id",
            "pid",
            "uid",
            "state_home",
            "heartbeat_path",
            "launch_mode",
            "cleanup_policy",
            "containment_created_by_tokenpak",
            "containment_method",
            "tokenpak_version",
            "launcher_version",
        }
        missing = required - data.keys()
        if missing:
            raise ManifestValidationError(f"manifest missing required keys: {sorted(missing)}")
        kwargs = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**kwargs)


__all__: list[str] = []
