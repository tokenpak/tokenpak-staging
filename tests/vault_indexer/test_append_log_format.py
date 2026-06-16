"""Append-log writer — record-shape contract + 4096-byte cap + rotation tests.

Writer-side only (no indexer, no benchmark). Fixtures are deliberately neutral —
no hostnames, identities, or absolute personal paths — so the public
sensitive-content scan stays clean.
"""
from datetime import datetime, timezone

import pytest

from tokenpak.vault import append_log as al


def _proxy_payload():
    return {
        "request_frame_digest": "a" * 64,
        "output_text": "example output",
        "model_id": "example-model-v1",
        "token_usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "request_id": "req-0001",
    }


# --- record shape ------------------------------------------------------------
def test_record_has_closed_minimum_field_set():
    rec = al.build_record(
        "proxy_output", _proxy_payload(),
        source_adapter_id="proxy",
        event_id="00000000-0000-7000-8000-000000000001",
        created_at=datetime(2026, 5, 20, 17, 33, 21, 412000, tzinfo=timezone.utc),
    )
    assert rec["schema_version"] == al.SCHEMA_VERSION
    assert rec["event_id"] == "00000000-0000-7000-8000-000000000001"
    assert rec["event_type"] == "proxy_output"
    assert rec["created_at"] == "2026-05-20T17:33:21.412Z"
    assert rec["source"] == {"adapter_id": "proxy", "adapter_version": "v1"}
    assert rec["payload"] == _proxy_payload()
    assert len(rec["payload_sha256"]) == 64
    assert set(rec) == {
        "schema_version", "event_id", "event_type", "created_at",
        "source", "payload", "payload_sha256",
    }


def test_event_type_is_closed_set():
    with pytest.raises(ValueError):
        al.build_record("unknown_event", {}, source_adapter_id="proxy")


def test_generated_event_id_is_uuid7_time_ordered():
    earlier = al._uuid7(now_ms=1_000_000)
    later = al._uuid7(now_ms=2_000_000)
    assert earlier[14] == "7"  # version nibble
    # a later timestamp sorts after an earlier one (same-ms ordering is random,
    # so order is only guaranteed across distinct millisecond timestamps)
    assert earlier < later

    rec = al.build_record("vault_edit", {"path": "docs/a.md", "op": "create"}, source_adapter_id="vault-watcher")
    assert rec["event_id"][14] == "7"  # generated ids are version 7 too


def test_payload_sha256_is_canonical_key_order_independent():
    p1 = {"a": 1, "b": {"x": 1, "y": 2}}
    p2 = {"b": {"y": 2, "x": 1}, "a": 1}
    assert al.payload_sha256(p1) == al.payload_sha256(p2)


# --- 4096-byte cap -----------------------------------------------------------
def test_record_under_cap_is_written_and_read_back(tmp_path):
    rec = al.build_record("proxy_output", _proxy_payload(), source_adapter_id="proxy",
                          created_at=datetime(2026, 5, 20, tzinfo=timezone.utc))
    res = al.AppendLogWriter(tmp_path).write(rec)
    assert res.ok and res.written and res.n_bytes <= al.MAX_RECORD_BYTES
    assert al.read_records(res.path) == [rec]


def test_oversized_record_fails_closed(tmp_path):
    big = al.build_record("proxy_output", {"output_text": "x" * 5000}, source_adapter_id="proxy",
                          created_at=datetime(2026, 5, 20, tzinfo=timezone.utc))
    res = al.AppendLogWriter(tmp_path).write(big)
    assert not res.written
    assert res.stop_reason == "oversized-record"
    assert list(tmp_path.iterdir()) == []  # nothing written


# --- rotation + append atomicity --------------------------------------------
def test_daily_rotation_one_segment_per_utc_date(tmp_path):
    w = al.AppendLogWriter(tmp_path)
    w.write(al.build_record("vault_edit", {"path": "docs/a.md", "op": "create"},
                            source_adapter_id="vault-watcher", created_at=datetime(2026, 5, 20, tzinfo=timezone.utc)))
    w.write(al.build_record("vault_edit", {"path": "docs/b.md", "op": "create"},
                            source_adapter_id="vault-watcher", created_at=datetime(2026, 5, 21, tzinfo=timezone.utc)))
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["2026-05-20.ndjson", "2026-05-21.ndjson"]


def test_appends_accumulate_in_one_segment(tmp_path):
    w = al.AppendLogWriter(tmp_path)
    for i in range(3):
        w.write(al.build_record("transcript_line", {"line": f"l{i}"}, source_adapter_id="transcript-adapter",
                                created_at=datetime(2026, 5, 20, tzinfo=timezone.utc)))
    recs = al.read_records(tmp_path / "2026-05-20.ndjson")
    assert len(recs) == 3


def test_read_records_tolerates_torn_trailing_line(tmp_path):
    seg = tmp_path / "2026-05-20.ndjson"
    good = al.serialize(al.build_record("transcript_line", {"line": "ok"}, source_adapter_id="transcript-adapter",
                                        created_at=datetime(2026, 5, 20, tzinfo=timezone.utc)))
    seg.write_bytes(good + b'{"partial": ')  # crash mid-write of a second record
    recs = al.read_records(seg)
    assert len(recs) == 1 and recs[0]["payload"] == {"line": "ok"}


# --- feature flag default off ------------------------------------------------
def test_emit_event_is_noop_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("TOKENPAK_APPEND_LOG", raising=False)
    res = al.emit_event("vault_edit", {"path": "docs/a.md", "op": "create"},
                        source_adapter_id="vault-watcher", directory=tmp_path)
    assert res.ok and not res.written and res.stop_reason == "disabled"
    assert list(tmp_path.iterdir()) == []


def test_emit_event_writes_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_APPEND_LOG", "1")
    res = al.emit_event("vault_edit", {"path": "docs/a.md", "op": "create"},
                        source_adapter_id="vault-watcher", directory=tmp_path,
                        created_at=datetime(2026, 5, 20, tzinfo=timezone.utc))
    assert res.written and res.path.name == "2026-05-20.ndjson"
