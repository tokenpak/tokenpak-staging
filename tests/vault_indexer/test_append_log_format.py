"""Append-log record-format invariants (design §1, AC-impl-1, AC-impl-2 input).

The contracts under test:

- Required-fields presence (§1.3).
- ``event_type`` is a closed set.
- Records are NDJSON (one record per line, UTF-8, newline-terminated).
- ``payload_sha256`` is the deterministic SHA-256 of the canonical-form
  payload.
- Records ≤ 4096 bytes including newline; oversized payloads raise
  ``OversizedRecordError`` with no on-disk write.
- The segment file is named ``YYYY-MM-DD.ndjson`` in UTC.
- ``O_APPEND`` keeps concurrent writers' lines from interleaving.
- Repeated minting within the same millisecond stays UUIDv7-monotone.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokenpak.vault.indexer import append_log
from tokenpak.vault.indexer.append_log import (
    MAX_RECORD_BYTES,
    InvalidEventTypeError,
    OversizedRecordError,
    build_record,
    canonical_payload_sha256,
    iter_segment,
    reset_writer_state,
    write_record,
)


@pytest.fixture(autouse=True)
def _isolate_writer_state() -> None:
    reset_writer_state()
    yield
    reset_writer_state()


@pytest.fixture
def segment_dir(tmp_path: Path) -> Path:
    d = tmp_path / "append-log"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _proxy_payload(output_text: str = "ok") -> dict[str, object]:
    return {
        "request_frame_digest": "a" * 64,
        "output_text": output_text,
        "model_id": "test-model",
        "token_usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        "request_id": "req-1",
    }


# --- §1.3 required fields -------------------------------------------------


def test_build_record_carries_required_fields() -> None:
    rec = build_record("proxy_output", "proxy", "v1", _proxy_payload())
    required = {
        "schema_version",
        "event_id",
        "event_type",
        "created_at",
        "source",
        "payload",
        "payload_sha256",
    }
    assert required.issubset(rec.keys())
    assert rec["schema_version"] == 1
    assert rec["event_type"] == "proxy_output"
    assert set(rec["source"].keys()) == {"adapter_id", "adapter_version"}
    assert rec["source"]["adapter_id"] == "proxy"
    assert rec["source"]["adapter_version"] == "v1"


def test_payload_sha256_is_canonical() -> None:
    payload = _proxy_payload()
    rec = build_record("proxy_output", "proxy", "v1", payload)
    assert rec["payload_sha256"] == canonical_payload_sha256(payload)
    # Same payload with re-ordered keys yields the same SHA-256.
    reordered = {k: payload[k] for k in reversed(list(payload.keys()))}
    assert canonical_payload_sha256(reordered) == rec["payload_sha256"]


def test_created_at_is_iso_ms_utc_z() -> None:
    rec = build_record(
        "proxy_output",
        "proxy",
        "v1",
        _proxy_payload(),
        now=datetime(2026, 5, 20, 17, 33, 21, 412_000, tzinfo=timezone.utc),
    )
    assert rec["created_at"] == "2026-05-20T17:33:21.412Z"


# --- §1.3 closed-set event_type ------------------------------------------


@pytest.mark.parametrize(
    "good", ["proxy_output", "vault_edit", "transcript_line"]
)
def test_valid_event_types_pass(good: str) -> None:
    build_record(good, "x", "v1", {"k": "v"})


def test_invalid_event_type_rejected() -> None:
    with pytest.raises(InvalidEventTypeError):
        build_record("unknown_type", "x", "v1", {"k": "v"})


# --- §1.2 record-format invariants ---------------------------------------


def test_write_record_writes_ndjson_line(segment_dir: Path) -> None:
    result = write_record(
        "proxy_output",
        "proxy",
        "v1",
        _proxy_payload(),
        segment_dir=segment_dir,
        now=datetime(2026, 5, 20, 0, 0, 0, tzinfo=timezone.utc),
    )

    assert result.segment_path.exists()
    assert result.segment_path.name == "2026-05-20.ndjson"
    raw = result.segment_path.read_bytes()
    assert raw.endswith(b"\n")
    parsed = json.loads(raw.rstrip(b"\n").decode("utf-8"))
    assert parsed["event_id"] == result.event_id
    assert parsed["payload_sha256"] == result.payload_sha256


def test_record_size_capped_at_4096_bytes(segment_dir: Path) -> None:
    # Craft a payload that pushes us over the cap.
    big = "x" * (MAX_RECORD_BYTES + 100)
    with pytest.raises(OversizedRecordError) as excinfo:
        write_record(
            "proxy_output",
            "proxy",
            "v1",
            _proxy_payload(output_text=big),
            segment_dir=segment_dir,
        )
    assert excinfo.value.size > MAX_RECORD_BYTES

    # Critically: no on-disk write happened.
    assert not list(segment_dir.glob("*.ndjson"))


def test_record_at_cap_succeeds(segment_dir: Path) -> None:
    # Build a payload exactly engineered to land near the cap. We probe by
    # walking down from a known-too-big size until success — keeps the
    # test robust to schema additions.
    for cushion in range(0, 4096):
        payload = _proxy_payload(output_text="a" * (3500 - cushion))
        from tokenpak.vault.indexer.append_log import _encode_record, build_record

        rec = build_record("proxy_output", "proxy", "v1", payload)
        blob = _encode_record(rec)
        if len(blob) <= MAX_RECORD_BYTES:
            break
    else:  # pragma: no cover — guards an unreachable branch
        pytest.fail("could not assemble a record under the cap")

    result = write_record(
        "proxy_output",
        "proxy",
        "v1",
        payload,
        segment_dir=segment_dir,
    )
    assert result.record_bytes <= MAX_RECORD_BYTES


# --- §1.1 daily-rotated segment filename ---------------------------------


def test_segment_filename_is_utc_dated(segment_dir: Path) -> None:
    result = write_record(
        "proxy_output",
        "proxy",
        "v1",
        _proxy_payload(),
        segment_dir=segment_dir,
        now=datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
    )
    assert result.segment_path.name == "2026-12-31.ndjson"


def test_day_rollover_opens_fresh_segment(segment_dir: Path) -> None:
    write_record(
        "proxy_output",
        "proxy",
        "v1",
        _proxy_payload(output_text="a"),
        segment_dir=segment_dir,
        now=datetime(2026, 5, 20, 23, 59, 59, tzinfo=timezone.utc),
    )
    write_record(
        "proxy_output",
        "proxy",
        "v1",
        _proxy_payload(output_text="b"),
        segment_dir=segment_dir,
        now=datetime(2026, 5, 21, 0, 0, 1, tzinfo=timezone.utc),
    )

    assert (segment_dir / "2026-05-20.ndjson").exists()
    assert (segment_dir / "2026-05-21.ndjson").exists()


# --- O_APPEND concurrency: lines never interleave -------------------------


def test_concurrent_writes_dont_interleave_lines(segment_dir: Path) -> None:
    N_THREADS = 8
    PER_THREAD = 50

    barrier = threading.Barrier(N_THREADS)

    def worker(tag: str) -> None:
        barrier.wait()
        for i in range(PER_THREAD):
            write_record(
                "proxy_output",
                f"proxy-{tag}",
                "v1",
                _proxy_payload(output_text=f"{tag}-{i}"),
                segment_dir=segment_dir,
                now=datetime(2026, 5, 20, 0, 0, 0, tzinfo=timezone.utc),
            )

    threads = [
        threading.Thread(target=worker, args=(f"t{n}",)) for n in range(N_THREADS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    seg = segment_dir / "2026-05-20.ndjson"
    raw = seg.read_bytes()
    # Every line must parse standalone — no interleaved bytes.
    lines = raw.split(b"\n")
    assert lines[-1] == b""  # trailing newline
    parsed = [json.loads(line.decode("utf-8")) for line in lines[:-1]]
    assert len(parsed) == N_THREADS * PER_THREAD
    # All event_ids unique (UUIDv7 contract).
    ids = {p["event_id"] for p in parsed}
    assert len(ids) == len(parsed)


# --- UUIDv7 monotonicity within the same millisecond ----------------------


def test_uuid7_monotone_within_millisecond() -> None:
    ts = datetime(2026, 5, 20, 0, 0, 0, 500_000, tzinfo=timezone.utc)
    ids = [
        build_record("proxy_output", "proxy", "v1", {"i": i}, now=ts)["event_id"]
        for i in range(200)
    ]
    assert ids == sorted(ids), "UUIDv7 must be lexicographically monotone"
    assert len(set(ids)) == len(ids), "UUIDv7 must not collide within ms"


# --- iter_segment round-trip ---------------------------------------------


def test_iter_segment_round_trip(segment_dir: Path) -> None:
    written = []
    for i in range(5):
        r = write_record(
            "vault_edit",
            "vault-watcher",
            "v1",
            {"path": f"a/{i}.md", "op": "modify", "new_sha256": "f" * 64},
            segment_dir=segment_dir,
            now=datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc),
        )
        written.append(r.event_id)

    seg = segment_dir / "2026-05-20.ndjson"
    read_ids = [rec["event_id"] for _, rec in iter_segment(seg)]
    assert read_ids == written
