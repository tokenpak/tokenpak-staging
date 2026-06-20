# SPDX-License-Identifier: Apache-2.0
"""Tests for tokenpak.proxy.upstream_retry — record format and persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tokenpak.proxy.upstream_retry import (
    STATUS_DETERMINISTIC,
    STATUS_TERMINAL,
    UpstreamRetryRecord,
    delete_record_file,
    list_record_files,
    most_recent_failed,
    redact_headers,
    write_record,
)

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Route all tokenpak home lookups to a tmp directory."""
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "tpk"))
    monkeypatch.delenv("TOKENPAK_RETRY_PERSIST_BODY", raising=False)


# ── Header redaction ──────────────────────────────────────────────────────


def test_redact_headers_strips_api_key():
    headers = {"x-api-key": "sk-abc123", "content-type": "application/json"}
    out = redact_headers(headers)
    assert out["x-api-key"] == "[REDACTED]"
    assert out["content-type"] == "application/json"


def test_redact_headers_strips_authorization():
    headers = {"Authorization": "Bearer token123", "Accept": "application/json"}
    out = redact_headers(headers)
    assert out["Authorization"] == "[REDACTED]"
    assert out["Accept"] == "application/json"


def test_redact_headers_case_insensitive():
    headers = {
        "X-API-KEY": "secret",
        "AUTHORIZATION": "Bearer x",
        "Proxy-Authorization": "Basic y",
        "Cookie": "session=z",
        "X-Auth-Token": "tok",
        "X-Forwarded-Authorization": "Bearer w",
    }
    out = redact_headers(headers)
    for k in headers:
        assert out[k] == "[REDACTED]", f"expected redaction for {k!r}"


def test_redact_headers_non_credential_unchanged():
    headers = {"content-type": "application/json", "x-request-id": "req-1"}
    out = redact_headers(headers)
    assert out == headers


# ── write_record persists safely ─────────────────────────────────────────


def test_write_record_creates_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "tpk"))
    r = write_record(
        request_id="req-001",
        tip_plan_id="plan-abc",
        endpoint="/v1/messages",
        provider="anthropic",
        model="claude-sonnet-4-5",
        headers={"x-api-key": "sk-secret", "content-type": "application/json"},
        body=b'{"messages":[{"role":"user","content":"hello"}]}',
    )
    assert r.request_id == "req-001"
    assert r.tip_plan_id == "plan-abc"
    assert r.headers_redacted["x-api-key"] == "[REDACTED]"
    assert r.headers_redacted["content-type"] == "application/json"
    assert r.body_hash is not None
    assert r.body_hash.startswith("sha256:")
    assert r.body_preview is not None
    assert r.body_full is None  # not persisted without env var


def test_write_record_no_full_body_without_env_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "tpk"))
    write_record(
        request_id="req-002",
        endpoint="/v1/messages",
        body=b"sensitive request body content",
    )
    items = list_record_files()
    assert len(items) == 1
    path, _ = items[0]
    on_disk = json.loads(path.read_text())
    assert on_disk["body_full"] is None  # body_full never written; body_preview is still shown


def test_write_record_persists_full_body_with_env_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "tpk"))
    monkeypatch.setenv("TOKENPAK_RETRY_PERSIST_BODY", "1")
    body_text = b"full body content for recovery"
    write_record(
        request_id="req-003",
        endpoint="/v1/messages",
        body=body_text,
    )
    items = list_record_files()
    _, r = items[0]
    assert r.body_full == body_text.decode()
    assert r.body_persisted is True


def test_write_record_empty_body(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "tpk"))
    r = write_record(request_id="req-004", endpoint="/v1/messages")
    assert r.body_hash is None
    assert r.body_preview is None
    assert r.body_full is None


def test_write_record_never_leaks_credential_headers_on_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "tpk"))
    write_record(
        request_id="req-005",
        endpoint="/v1/messages",
        headers={"Authorization": "Bearer supersecret", "x-api-key": "key-xyz"},
    )
    items = list_record_files()
    path, _ = items[0]
    content = path.read_text()
    assert "supersecret" not in content
    assert "key-xyz" not in content


# ── list_record_files ordering ────────────────────────────────────────────


def test_list_record_files_sorted_oldest_first(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "tpk"))
    write_record(request_id="req-A", endpoint="/v1/messages")
    write_record(request_id="req-B", endpoint="/v1/messages")
    write_record(request_id="req-C", endpoint="/v1/messages")
    items = list_record_files()
    assert len(items) == 3
    ids = [r.request_id for _, r in items]
    # Files are sorted by filename (which includes timestamp prefix) → oldest first
    assert ids == sorted(ids) or ids[0] == "req-A"


def test_list_record_files_empty_when_no_records(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "tpk"))
    assert list_record_files() == []


def test_list_record_files_skips_malformed(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "tpk"))
    write_record(request_id="req-good", endpoint="/v1/messages")
    # Inject a malformed file
    from tokenpak import _paths
    bad = _paths.home() / "recovery" / "upstream" / "bad.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not valid json")
    items = list_record_files()
    assert len(items) == 1
    assert items[0][1].request_id == "req-good"


# ── most_recent_failed ────────────────────────────────────────────────────


def test_most_recent_failed_returns_none_when_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "tpk"))
    assert most_recent_failed() is None


def test_most_recent_failed_returns_last_record(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "tpk"))
    write_record(request_id="req-first", endpoint="/v1/messages")
    write_record(request_id="req-last", endpoint="/v1/messages")
    result = most_recent_failed()
    assert result is not None
    _, r = result
    assert r.request_id == "req-last"


# ── delete_record_file ────────────────────────────────────────────────────


def test_delete_record_file_removes_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "tpk"))
    write_record(request_id="req-del", endpoint="/v1/messages")
    items = list_record_files()
    assert len(items) == 1
    path, _ = items[0]
    assert delete_record_file(path) is True
    assert not path.exists()
    assert list_record_files() == []


def test_delete_record_file_returns_false_for_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "tpk"))
    ghost = tmp_path / "tpk" / "recovery" / "upstream" / "ghost.json"
    assert delete_record_file(ghost) is False


# ── Deterministic-failure flag ─────────────────────────────────────────────


def test_deterministic_failure_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "tpk"))
    r = write_record(
        request_id="req-det",
        endpoint="/v1/messages",
        terminal_recovery_status=STATUS_DETERMINISTIC,
    )
    assert r.is_deterministic_failure() is True


def test_non_deterministic_is_not_flagged(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "tpk"))
    r = write_record(
        request_id="req-term",
        endpoint="/v1/messages",
        terminal_recovery_status=STATUS_TERMINAL,
    )
    assert r.is_deterministic_failure() is False


# ── safe_dict never includes body_full ────────────────────────────────────


def test_safe_dict_excludes_body_full(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "tpk"))
    monkeypatch.setenv("TOKENPAK_RETRY_PERSIST_BODY", "1")
    r = write_record(
        request_id="req-safe",
        endpoint="/v1/messages",
        body=b"sensitive full body",
    )
    d = r.safe_dict()
    assert d["body_full"] is None  # body_full zeroed; body_preview may still show preview text


# ── body_preview limited to 200 bytes ────────────────────────────────────


def test_body_preview_truncated_to_200_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "tpk"))
    long_body = b"x" * 1000
    r = write_record(request_id="req-long", endpoint="/v1/messages", body=long_body)
    assert r.body_preview is not None
    assert len(r.body_preview) <= 200
