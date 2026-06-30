# SPDX-License-Identifier: Apache-2.0
"""Tests for the Runtime Hygiene registry foundation.

Covers the manifest schema + validation, lifecycle transition table,
command-shape redaction, atomic/permissioned manifest persistence, fail-closed
reads, and the registry-aware ``register_session`` write-failure policy
(cleanup-capable aborts; non-cleanup downgrades to never_touch).
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from tokenpak.runtime import hygiene
from tokenpak.runtime import hygiene_registry as reg
from tokenpak.runtime import hygiene_schema as schema
from tokenpak.runtime.hygiene_schema import (
    CleanupPolicy,
    ContainmentMethod,
    InvalidLifecycleTransition,
    LaunchMode,
    Lifecycle,
    ManifestValidationError,
    SessionManifest,
)


@pytest.fixture
def tpk_home(monkeypatch, tmp_path):
    """Point the canonical TokenPak home at a tmp dir via TOKENPAK_HOME."""
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "tpk"))
    monkeypatch.delenv("HOME", raising=False)
    return tmp_path / "tpk"


def _manifest(**overrides) -> SessionManifest:
    base = dict(
        tokenpak_session_id="codex-abc123",
        pid=os.getpid(),
        uid=os.getuid(),
        state_home="/home/u/.codex",
        heartbeat_path="/home/u/.tpk/runtime/sessions/codex-abc123/heartbeat",
        launch_mode=LaunchMode.CODEX,
        cleanup_policy=CleanupPolicy.NEVER_TOUCH,
        containment_created_by_tokenpak=False,
        containment_method=ContainmentMethod.NONE,
        tokenpak_version="1.7.1",
        launcher_version="1",
    )
    base.update(overrides)
    return SessionManifest(**base)


# ── schema validation ─────────────────────────────────────────────────────


def test_valid_manifest_passes_validation():
    assert _manifest().validate() is not None


@pytest.mark.parametrize(
    "field,value",
    [
        ("launch_mode", "telepathy"),
        ("cleanup_policy", "delete_everything"),
        ("containment_method", "voodoo"),
        ("lifecycle", "ascended"),
        ("schema_version", 999),
        ("pid", 0),
        ("uid", -1),
        ("state_home", ""),
        ("tokenpak_session_id", ""),
    ],
)
def test_invalid_field_rejected(field, value):
    with pytest.raises(ManifestValidationError):
        _manifest(**{field: value}).validate()


def test_to_dict_round_trips_through_from_dict():
    m = _manifest(agent_id="trix", boot_id="boot-xyz", process_group_id=42)
    again = SessionManifest.from_dict(m.to_dict())
    assert again.to_dict() == m.to_dict()
    again.validate()


def test_from_dict_missing_required_key_fails_closed():
    d = _manifest().to_dict()
    del d["pid"]
    with pytest.raises(ManifestValidationError):
        SessionManifest.from_dict(d)


# ── command-shape redaction ────────────────────────────────────────────────


def test_redaction_keeps_flag_names_drops_values():
    shape = schema.redact_command_shape(
        ["codex", "-p", "tok-secret", "--model=gpt-5", "/tmp/private"]
    )
    assert shape == "codex -p <arg> --model <arg>"
    assert "tok-secret" not in shape
    assert "gpt-5" not in shape
    assert "/tmp/private" not in shape


def test_redaction_accepts_string_and_strips_paths():
    shape = schema.redact_command_shape("/usr/bin/claude --resume /tmp/secret")
    assert shape == "claude --resume <arg>"
    assert "secret" not in shape


def test_redaction_handles_empty_and_none():
    assert schema.redact_command_shape(None) == ""
    assert schema.redact_command_shape([]) == ""
    assert schema.redact_command_shape("") == ""


def test_redaction_caps_token_count():
    shape = schema.redact_command_shape(["prog"] + ["x"] * 100)
    assert shape.endswith("...")
    # cap is 32 tokens; basename + (cap-1) args + ellipsis
    assert len(shape.split()) <= 33


# ── lifecycle transitions ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "old,new",
    [
        (Lifecycle.ACTIVE, Lifecycle.CLOSING),
        (Lifecycle.ACTIVE, Lifecycle.CLEANUP_ATTEMPTED),
        (Lifecycle.CLOSING, Lifecycle.CLOSED),
        (Lifecycle.CLEANUP_ATTEMPTED, Lifecycle.CLOSED),
        (Lifecycle.CLEANUP_ATTEMPTED, Lifecycle.CLEANUP_FAILED),
        (Lifecycle.CLEANUP_ATTEMPTED, Lifecycle.RECEIPT_FAILED),
    ],
)
def test_valid_transitions_allowed(old, new):
    assert schema.is_valid_transition(old, new) is True
    schema.assert_transition(old, new)  # must not raise


@pytest.mark.parametrize(
    "old,new",
    [
        (Lifecycle.ACTIVE, Lifecycle.CLOSED),  # skips closing
        (Lifecycle.CLOSED, Lifecycle.ACTIVE),  # terminal -> anything
        (Lifecycle.CLEANUP_FAILED, Lifecycle.CLEANUP_ATTEMPTED),  # terminal
        (Lifecycle.ACTIVE, Lifecycle.ACTIVE),  # self-transition
        (Lifecycle.CLOSING, Lifecycle.CLEANUP_ATTEMPTED),
    ],
)
def test_invalid_transitions_fail_closed(old, new):
    assert schema.is_valid_transition(old, new) is False
    with pytest.raises(InvalidLifecycleTransition):
        schema.assert_transition(old, new)


def test_assert_transition_rejects_unknown_states():
    with pytest.raises(InvalidLifecycleTransition):
        schema.assert_transition("active", "bogus")
    with pytest.raises(InvalidLifecycleTransition):
        schema.assert_transition("bogus", "closed")


# ── registry: paths resolve through _paths ─────────────────────────────────


def test_paths_resolve_under_canonical_home(tpk_home):
    p = reg.manifest_path("codex-s1")
    assert p == tpk_home / "runtime" / "sessions" / "codex-s1" / "manifest.json"


def test_session_id_path_traversal_rejected(tpk_home):
    for bad in ["../escape", "a/b", "", ".", ".."]:
        with pytest.raises(ValueError):
            reg.session_dir(bad)


# ── registry: atomic + permissioned write ──────────────────────────────────


def test_write_creates_0700_dirs_and_0600_file(tpk_home):
    path = reg.write_manifest(_manifest(tokenpak_session_id="codex-perm"))
    file_mode = stat.S_IMODE(path.stat().st_mode)
    assert file_mode == 0o600
    # every directory from the session dir up to (and including) the home root
    for d in (path.parent, path.parent.parent, path.parent.parent.parent, tpk_home):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d


def test_write_then_read_round_trip(tpk_home):
    m = _manifest(tokenpak_session_id="codex-rt", agent_id="trix")
    reg.write_manifest(m)
    loaded = reg.read_manifest("codex-rt")
    assert loaded is not None
    assert loaded.tokenpak_session_id == "codex-rt"
    assert loaded.agent_id == "trix"
    assert loaded.lifecycle == Lifecycle.ACTIVE


def test_write_is_valid_json_and_leaves_no_tmp(tpk_home):
    path = reg.write_manifest(_manifest(tokenpak_session_id="codex-json"))
    json.loads(path.read_text())  # parses
    leftovers = [p for p in path.parent.iterdir() if p.name.startswith(".manifest.")]
    assert leftovers == []


def test_write_failure_raises_and_cleans_tmp(tpk_home, monkeypatch):
    # Simulate a rename failure mid-write: the manifest must not appear and the
    # temp file must be cleaned up (no partial manifest left behind).
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(reg.os, "replace", boom)
    with pytest.raises(reg.ManifestWriteError):
        reg.write_manifest(_manifest(tokenpak_session_id="codex-fail"))
    sdir = reg.session_dir("codex-fail")
    assert not reg.manifest_path("codex-fail").exists()
    leftovers = [p for p in sdir.iterdir() if p.name.startswith(".manifest.")]
    assert leftovers == []


def test_read_missing_returns_none(tpk_home):
    assert reg.read_manifest("codex-never") is None


def test_read_corrupt_fails_closed(tpk_home):
    sdir = reg.ensure_session_dir("codex-corrupt")
    (sdir / "manifest.json").write_text("{not valid json")
    with pytest.raises(reg.ManifestReadError):
        reg.read_manifest("codex-corrupt")


# ── registry: persisted lifecycle transition ───────────────────────────────


def test_transition_persists_new_state(tpk_home):
    reg.write_manifest(_manifest(tokenpak_session_id="codex-tr"))
    updated = reg.transition("codex-tr", Lifecycle.CLOSING)
    assert updated.lifecycle == Lifecycle.CLOSING
    assert updated.lifecycle_updated_at is not None
    assert reg.read_manifest("codex-tr").lifecycle == Lifecycle.CLOSING


def test_transition_rejects_invalid_edge(tpk_home):
    reg.write_manifest(_manifest(tokenpak_session_id="codex-bad"))
    with pytest.raises(InvalidLifecycleTransition):
        reg.transition("codex-bad", Lifecycle.CLOSED)  # active -> closed skips closing
    # the on-disk lifecycle is unchanged
    assert reg.read_manifest("codex-bad").lifecycle == Lifecycle.ACTIVE


def test_transition_missing_manifest_raises(tpk_home):
    with pytest.raises(reg.ManifestReadError):
        reg.transition("codex-absent", Lifecycle.CLOSING)


# ── hygiene.register_session: write-failure policy + authority gate ─────────


def test_register_non_cleanup_writes_manifest(tpk_home):
    res = hygiene.register_session(
        session_id="codex-reg",
        launch_mode=LaunchMode.CODEX,
        cleanup_policy=CleanupPolicy.NEVER_TOUCH,
        state_home="/home/u/.codex",
        command=["codex", "--model", "gpt-5"],
        tokenpak_version="1.7.1",
    )
    assert res.registered is True
    assert res.cleanup_policy == CleanupPolicy.NEVER_TOUCH
    assert res.manifest_path is not None
    on_disk = reg.read_manifest("codex-reg")
    assert on_disk is not None
    assert on_disk.command_shape == "codex --model <arg>"


def test_register_term_without_containment_is_rejected(tpk_home):
    with pytest.raises(ValueError):
        hygiene.register_session(
            session_id="codex-term",
            launch_mode=LaunchMode.CLAUDE,
            cleanup_policy=CleanupPolicy.TERM_ALLOWED,
            state_home="/home/u/.codex",
            tokenpak_version="1.7.1",
            # no TokenPak-created containment → a pgrp match alone is not authority
        )


def test_register_term_with_containment_succeeds(tpk_home):
    res = hygiene.register_session(
        session_id="codex-term-ok",
        launch_mode=LaunchMode.CLAUDE,
        cleanup_policy=CleanupPolicy.TERM_ALLOWED,
        state_home="/home/u/.codex",
        tokenpak_version="1.7.1",
        containment_created_by_tokenpak=True,
        containment_method=ContainmentMethod.SYSTEMD_SCOPE,
        containment_id="tokenpak-sess.scope",
    )
    assert res.registered is True
    assert res.cleanup_policy == CleanupPolicy.TERM_ALLOWED


def test_non_cleanup_write_failure_downgrades_to_never_touch(tpk_home, monkeypatch):
    def boom(_manifest):
        raise reg.ManifestWriteError("no space")

    monkeypatch.setattr(hygiene._registry, "write_manifest", boom)
    res = hygiene.register_session(
        session_id="codex-degrade",
        launch_mode=LaunchMode.CODEX,
        cleanup_policy=CleanupPolicy.REPORT_ONLY,
        state_home="/home/u/.codex",
        tokenpak_version="1.7.1",
    )
    assert res.registered is False
    assert res.cleanup_policy == CleanupPolicy.NEVER_TOUCH
    assert res.manifest_path is None


def test_cleanup_capable_write_failure_aborts(tpk_home, monkeypatch):
    def boom(_manifest):
        raise reg.ManifestWriteError("no space")

    monkeypatch.setattr(hygiene._registry, "write_manifest", boom)
    with pytest.raises(reg.ManifestWriteError):
        hygiene.register_session(
            session_id="codex-abort",
            launch_mode=LaunchMode.CLAUDE,
            cleanup_policy=CleanupPolicy.TERM_ALLOWED,
            state_home="/home/u/.codex",
            tokenpak_version="1.7.1",
            containment_created_by_tokenpak=True,
            containment_method=ContainmentMethod.SYSTEMD_SCOPE,
            containment_id="tokenpak-sess.scope",
        )


def test_collect_identity_records_pid_and_uid(tpk_home):
    ident = hygiene.collect_identity()
    assert ident.pid == os.getpid()
    assert ident.uid == os.getuid()
