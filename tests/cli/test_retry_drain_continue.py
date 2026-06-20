# SPDX-License-Identifier: Apache-2.0
"""Tests for ``tokenpak retry drain`` and ``tokenpak codex continue --last-failed``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from tokenpak.proxy.upstream_retry import (
    STATUS_DETERMINISTIC,
    STATUS_TERMINAL,
    list_record_files,
    write_record,
)

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "tpk"))
    monkeypatch.delenv("TOKENPAK_RETRY_PERSIST_BODY", raising=False)


def _drain_args(**kwargs) -> argparse.Namespace:
    defaults = {"visible_turn": False, "json": False, "dry_run": False}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ── retry drain — basic listing ───────────────────────────────────────────


def test_drain_empty_queue(capsys):
    from tokenpak.cli.commands.retry_cmd import cmd_retry_drain

    rc = cmd_retry_drain(_drain_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "No pending" in out


def test_drain_empty_queue_json(capsys):
    from tokenpak.cli.commands.retry_cmd import cmd_retry_drain

    rc = cmd_retry_drain(_drain_args(json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "empty"
    assert data["drained"] == 0


# ── retry drain — skips records requiring visible continuation ─────────────


def test_drain_skips_visible_continuation_by_default(capsys):
    from tokenpak.cli.commands.retry_cmd import cmd_retry_drain

    write_record(
        request_id="req-vis",
        endpoint="/v1/messages",
        visible_continuation_required=True,
        terminal_recovery_status=STATUS_TERMINAL,
    )
    rc = cmd_retry_drain(_drain_args())
    assert rc == 0
    # Record must still exist on disk
    assert len(list_record_files()) == 1
    out = capsys.readouterr().out
    assert "req-vis" in out
    assert "requires_visible_turn" in out


def test_drain_processes_visible_continuation_with_flag(capsys):
    from tokenpak.cli.commands.retry_cmd import cmd_retry_drain

    write_record(
        request_id="req-vis2",
        endpoint="/v1/messages",
        visible_continuation_required=True,
        terminal_recovery_status=STATUS_TERMINAL,
    )
    rc = cmd_retry_drain(_drain_args(visible_turn=True))
    assert rc == 0
    assert list_record_files() == []


# ── retry drain — deterministic failures are never drained ────────────────


def test_drain_never_drains_deterministic_failure(capsys):
    from tokenpak.cli.commands.retry_cmd import cmd_retry_drain

    write_record(
        request_id="req-det",
        endpoint="/v1/messages",
        terminal_recovery_status=STATUS_DETERMINISTIC,
        visible_continuation_required=False,
    )
    rc = cmd_retry_drain(_drain_args(visible_turn=True))
    assert rc == 0
    # Must still be on disk
    assert len(list_record_files()) == 1
    out = capsys.readouterr().out
    assert "deterministic_failure" in out


# ── retry drain — drains eligible records ─────────────────────────────────


def test_drain_removes_eligible_record(capsys):
    from tokenpak.cli.commands.retry_cmd import cmd_retry_drain

    write_record(
        request_id="req-ok",
        endpoint="/v1/messages",
        visible_continuation_required=False,
        terminal_recovery_status=STATUS_TERMINAL,
    )
    rc = cmd_retry_drain(_drain_args())
    assert rc == 0
    assert list_record_files() == []
    out = capsys.readouterr().out
    assert "req-ok" in out


def test_drain_dry_run_does_not_delete(capsys):
    from tokenpak.cli.commands.retry_cmd import cmd_retry_drain

    write_record(
        request_id="req-dryrun",
        endpoint="/v1/messages",
        visible_continuation_required=False,
        terminal_recovery_status=STATUS_TERMINAL,
    )
    rc = cmd_retry_drain(_drain_args(dry_run=True))
    assert rc == 0
    assert len(list_record_files()) == 1  # not deleted


def test_drain_json_output_structure(capsys):
    from tokenpak.cli.commands.retry_cmd import cmd_retry_drain

    write_record(
        request_id="req-json-drain",
        endpoint="/v1/messages",
        visible_continuation_required=False,
        terminal_recovery_status=STATUS_TERMINAL,
    )
    write_record(
        request_id="req-json-skip",
        endpoint="/v1/messages",
        visible_continuation_required=True,
        terminal_recovery_status=STATUS_TERMINAL,
    )
    rc = cmd_retry_drain(_drain_args(json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["drained"] == 1
    assert data["skipped"] == 1
    drained_ids = [r["request_id"] for r in data["records"]["drained"]]
    skipped_ids = [r["request_id"] for r in data["records"]["skipped"]]
    assert "req-json-drain" in drained_ids
    assert "req-json-skip" in skipped_ids


# ── retry drain — request_id and tip_plan_id in output ────────────────────


def test_drain_output_includes_request_id_and_plan_id(capsys):
    from tokenpak.cli.commands.retry_cmd import cmd_retry_drain

    write_record(
        request_id="req-with-plan",
        tip_plan_id="plan-xyz-123",
        endpoint="/v1/messages",
        visible_continuation_required=False,
        terminal_recovery_status=STATUS_TERMINAL,
    )
    cmd_retry_drain(_drain_args())
    out = capsys.readouterr().out
    assert "req-with-plan" in out
    assert "plan-xyz-123" in out


# ── codex continue --last-failed ─────────────────────────────────────────


def test_continue_no_records_returns_1(capsys):
    from tokenpak.cli.commands.retry_cmd import cmd_codex_continue_last_failed

    rc = cmd_codex_continue_last_failed(["--last-failed"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "No recovery records" in out


def test_continue_last_failed_emits_visible_turn(capsys):
    from tokenpak.cli.commands.retry_cmd import cmd_codex_continue_last_failed

    write_record(
        request_id="req-cont",
        tip_plan_id="plan-cont-001",
        endpoint="/v1/messages",
        provider="anthropic",
        model="claude-sonnet-4-5",
        body=b'{"messages":[{"role":"user","content":"continue this"}]}',
        visible_continuation_required=True,
        terminal_recovery_status=STATUS_TERMINAL,
    )
    rc = cmd_codex_continue_last_failed(["--last-failed"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "req-cont" in out
    assert "plan-cont-001" in out
    assert "NEW visible turn" in out
    assert "hidden replay" in out


def test_continue_last_failed_never_includes_credential_headers(capsys):
    from tokenpak.cli.commands.retry_cmd import cmd_codex_continue_last_failed

    write_record(
        request_id="req-cred",
        endpoint="/v1/messages",
        headers={"Authorization": "Bearer supersecret", "x-api-key": "sk-private"},
        body=b"body content",
        visible_continuation_required=True,
    )
    cmd_codex_continue_last_failed(["--last-failed"])
    out = capsys.readouterr().out
    assert "supersecret" not in out
    assert "sk-private" not in out
    assert "[REDACTED]" in out


def test_continue_last_failed_json_output(capsys):
    from tokenpak.cli.commands.retry_cmd import cmd_codex_continue_last_failed

    write_record(
        request_id="req-json",
        tip_plan_id="plan-j",
        endpoint="/v1/messages",
        provider="anthropic",
    )
    rc = cmd_codex_continue_last_failed(["--last-failed", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["request_id"] == "req-json"
    assert data["tip_plan_id"] == "plan-j"
    assert data["body_full"] is None  # safe_dict never includes body_full


def test_continue_missing_body_produces_actionable_prompt(capsys):
    from tokenpak.cli.commands.retry_cmd import cmd_codex_continue_last_failed

    # Record with no body material (body_preview=None, body_hash=None)
    write_record(
        request_id="req-nobody",
        endpoint="/v1/messages",
        body=None,
        visible_continuation_required=True,
    )
    rc = cmd_codex_continue_last_failed(["--last-failed"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "req-nobody" in out
    # Should prompt user about next steps, not silently fail
    assert "Next steps" in out or "tokenpak codex" in out


def test_continue_missing_body_without_persist_flag_explains_how_to_capture(capsys):
    from tokenpak.cli.commands.retry_cmd import cmd_codex_continue_last_failed

    # body exists but body_persisted=False
    write_record(
        request_id="req-nopersist",
        endpoint="/v1/messages",
        body=b"some request body",
    )
    rc = cmd_codex_continue_last_failed(["--last-failed"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "TOKENPAK_RETRY_PERSIST_BODY" in out
