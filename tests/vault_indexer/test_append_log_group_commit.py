"""Group-commit fsync behaviour (design OQ-1 remediation).

The default durability mode appends without an inline fsync and relies on a
background thread to sync on a fixed cadence. These tests assert the
observable contract: records are immediately readable, the writer reports
``fsynced=False`` for the deferred path, ``flush_now()`` forces a sync, and
strict ``fsync=True`` still syncs inline.
"""

from __future__ import annotations

from pathlib import Path

from tokenpak.vault.indexer import append_log
from tokenpak.vault.indexer.append_log import (
    flush_now,
    iter_segment,
    reset_writer_state,
    write_record,
)


def _payload() -> dict[str, object]:
    return {"output_text": "hello", "request_id": "r-1"}


def test_default_mode_is_group_commit_not_inline_fsync(tmp_path: Path) -> None:
    reset_writer_state()
    res = write_record("proxy_output", "proxy", "v1", _payload(), segment_dir=tmp_path)
    # Deferred durability: no inline fsync happened on the hot path.
    assert res.fsynced is False
    reset_writer_state()


def test_record_is_readable_immediately_under_group_commit(tmp_path: Path) -> None:
    reset_writer_state()
    res = write_record("proxy_output", "proxy", "v1", _payload(), segment_dir=tmp_path)
    # Readable via the page cache before any fsync — group commit does not
    # delay visibility, only on-disk durability.
    records = [r for _, r in iter_segment(res.segment_path)]
    assert len(records) == 1
    assert records[0]["event_id"] == res.event_id
    reset_writer_state()


def test_flush_now_forces_sync_then_is_idempotent(tmp_path: Path) -> None:
    reset_writer_state()
    write_record("proxy_output", "proxy", "v1", _payload(), segment_dir=tmp_path)
    assert flush_now() is True  # pending write -> sync issued
    assert flush_now() is False  # nothing dirty -> no-op
    reset_writer_state()


def test_strict_fsync_true_syncs_inline(tmp_path: Path) -> None:
    reset_writer_state()
    res = write_record(
        "proxy_output", "proxy", "v1", _payload(), segment_dir=tmp_path, fsync=True
    )
    assert res.fsynced is True
    reset_writer_state()


def test_fsync_false_defers_without_marking_dirty_group(tmp_path: Path) -> None:
    reset_writer_state()
    res = write_record(
        "proxy_output", "proxy", "v1", _payload(), segment_dir=tmp_path, fsync=False
    )
    assert res.fsynced is False
    # fsync=False is the caller-owns-durability path; no group-commit sync is
    # promised, so flush_now reports nothing pending from this write.
    assert flush_now() is False
    reset_writer_state()


def test_configure_group_commit_interval_validates(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError):
        append_log.configure_group_commit(interval_ms=0)
    append_log.configure_group_commit(interval_ms=50)
    append_log.configure_group_commit(
        interval_ms=append_log.DEFAULT_GROUP_COMMIT_INTERVAL_MS
    )
