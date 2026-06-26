# SPDX-License-Identifier: Apache-2.0
"""``RecallStore.upsert_pak`` — happy path and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from tokenpak.companion.recall import PAK_STATUS_VALUES, RecallStore
from tokenpak.companion.recall.store import UpsertResult

_BASE_ROW = {
    "pak_id": "vault://block/auth-pattern",
    "pak_type": "vault",
    "source_type": "doc",
    "authority": "llm_generated",
    "title": "router-not-vault credential architecture",
    "content_hash": "0123456789abcdef" * 4,
    "summary": "Single-refresh-owner invariant across providers.",
    "project": "tokenpak",
    "topic": "creds",
}


def test_upsert_inserts_new_row(tmp_path: Path, require_fts5: None) -> None:
    """A first call inserts a row and reports ``inserted=True``."""
    db_path = tmp_path / "recall.db"
    with RecallStore.open(db_path) as store:
        result = store.upsert_pak(**_BASE_ROW)
        row = store.conn.execute(
            "SELECT pak_id, pak_type, project, topic, source_type, authority, "
            "title, summary, content_hash, superseded_by "
            "FROM paks WHERE pak_id = ?",
            (_BASE_ROW["pak_id"],),
        ).fetchone()

    assert isinstance(result, UpsertResult)
    assert result.inserted is True
    assert result.body_changed is False
    assert result.pak_id == _BASE_ROW["pak_id"]

    assert row == (
        _BASE_ROW["pak_id"],
        _BASE_ROW["pak_type"],
        _BASE_ROW["project"],
        _BASE_ROW["topic"],
        _BASE_ROW["source_type"],
        _BASE_ROW["authority"],
        _BASE_ROW["title"],
        _BASE_ROW["summary"],
        _BASE_ROW["content_hash"],
        None,  # superseded_by — left NULL when not provided
    )


def test_upsert_sets_timestamps(tmp_path: Path, require_fts5: None) -> None:
    """``created_at`` and ``updated_at`` are both populated on first insert."""
    db_path = tmp_path / "recall.db"
    with RecallStore.open(db_path) as store:
        store.upsert_pak(now="2026-05-11T12:00:00Z", **_BASE_ROW)
        row = store.conn.execute(
            "SELECT created_at, updated_at FROM paks WHERE pak_id = ?",
            (_BASE_ROW["pak_id"],),
        ).fetchone()
    assert row == ("2026-05-11T12:00:00Z", "2026-05-11T12:00:00Z")


def test_upsert_default_summary_is_empty_string(
    tmp_path: Path, require_fts5: None
) -> None:
    """``summary`` defaults to ``""`` and is stored as such."""
    db_path = tmp_path / "recall.db"
    fields = {k: v for k, v in _BASE_ROW.items() if k != "summary"}
    with RecallStore.open(db_path) as store:
        store.upsert_pak(**fields)
        s = store.conn.execute(
            "SELECT summary FROM paks WHERE pak_id = ?",
            (_BASE_ROW["pak_id"],),
        ).fetchone()
    assert s == ("",)


def test_upsert_two_distinct_paks(tmp_path: Path, require_fts5: None) -> None:
    """Two distinct pak_ids yield two rows."""
    db_path = tmp_path / "recall.db"
    other = dict(_BASE_ROW)
    other["pak_id"] = "vault://block/other"
    other["title"] = "other heading"
    other["content_hash"] = "feedfacecafebabe" * 4
    with RecallStore.open(db_path) as store:
        r1 = store.upsert_pak(**_BASE_ROW)
        r2 = store.upsert_pak(**other)
        n = store.conn.execute("SELECT COUNT(*) FROM paks").fetchone()[0]
    assert r1.inserted is True
    assert r2.inserted is True
    assert n == 2


def test_upsert_optional_fields_stored_as_null(
    tmp_path: Path, require_fts5: None
) -> None:
    """``project`` / ``topic`` / ``superseded_by`` / ``status`` default to NULL."""
    db_path = tmp_path / "recall.db"
    minimal = {
        "pak_id": "vault://block/minimal",
        "pak_type": "vault",
        "source_type": "doc",
        "authority": "llm_generated",
        "title": "minimal",
        "content_hash": "00" * 16,
    }
    with RecallStore.open(db_path) as store:
        store.upsert_pak(**minimal)
        row = store.conn.execute(
            "SELECT project, topic, superseded_by, status FROM paks WHERE pak_id = ?",
            (minimal["pak_id"],),
        ).fetchone()
    assert row == (None, None, None, None)


def test_upsert_status_round_trips(tmp_path: Path, require_fts5: None) -> None:
    """An authored lifecycle status is stored and exposed on PakRow."""
    db_path = tmp_path / "recall.db"
    row = dict(_BASE_ROW)
    row["status"] = "accepted"
    with RecallStore.open(db_path) as store:
        store.upsert_pak(**row)
        got = store.get_pak(_BASE_ROW["pak_id"])
    assert got is not None
    assert got.status == "accepted"


def test_upsert_omitted_status_preserves_existing_status(
    tmp_path: Path, require_fts5: None
) -> None:
    """Older callers that omit status must not clear an existing lifecycle."""
    db_path = tmp_path / "recall.db"
    initial = dict(_BASE_ROW)
    initial["status"] = "accepted"
    update = dict(_BASE_ROW)
    update["summary"] = "updated summary"
    with RecallStore.open(db_path) as store:
        store.upsert_pak(**initial)
        store.upsert_pak(**update)
        got = store.get_pak(_BASE_ROW["pak_id"])
    assert got is not None
    assert got.summary == "updated summary"
    assert got.status == "accepted"


def test_upsert_explicit_none_clears_existing_status(
    tmp_path: Path, require_fts5: None
) -> None:
    """Passing ``status=None`` explicitly clears the lifecycle value."""
    db_path = tmp_path / "recall.db"
    initial = dict(_BASE_ROW)
    initial["status"] = "accepted"
    update = dict(_BASE_ROW)
    update["status"] = None
    with RecallStore.open(db_path) as store:
        store.upsert_pak(**initial)
        store.upsert_pak(**update)
        got = store.get_pak(_BASE_ROW["pak_id"])
    assert got is not None
    assert got.status is None


def test_upsert_superseded_by_defaults_status_to_superseded(
    tmp_path: Path, require_fts5: None
) -> None:
    """A direct supersession projection backfills status when no status is provided."""
    db_path = tmp_path / "recall.db"
    successor = dict(_BASE_ROW)
    successor["pak_id"] = "vault://block/successor"
    successor["title"] = "successor"
    old = dict(_BASE_ROW)
    old["pak_id"] = "vault://block/old"
    old["superseded_by"] = successor["pak_id"]
    with RecallStore.open(db_path) as store:
        store.upsert_pak(**successor)
        store.upsert_pak(**old)
        row = store.conn.execute(
            "SELECT superseded_by, status FROM paks WHERE pak_id = ?",
            (old["pak_id"],),
        ).fetchone()
    assert row == (successor["pak_id"], "superseded")


def test_upsert_rejects_invalid_status(tmp_path: Path, require_fts5: None) -> None:
    """Lifecycle status is nullable, but non-null values must be canonical."""
    db_path = tmp_path / "recall.db"
    bad = dict(_BASE_ROW)
    bad["status"] = "unknown"
    with RecallStore.open(db_path) as store:
        with pytest.raises(ValueError) as exc:
            store.upsert_pak(**bad)
    assert "status" in str(exc.value)
    assert "unknown" not in PAK_STATUS_VALUES


@pytest.mark.parametrize(
    "missing",
    [
        "pak_id",
        "pak_type",
        "source_type",
        "authority",
        "title",
        "content_hash",
    ],
)
def test_upsert_missing_required_field_raises(
    tmp_path: Path, require_fts5: None, missing: str
) -> None:
    """An empty / whitespace required field raises ``ValueError``."""
    db_path = tmp_path / "recall.db"
    bad = dict(_BASE_ROW)
    bad[missing] = "   "
    with RecallStore.open(db_path) as store:
        with pytest.raises(ValueError) as exc:
            store.upsert_pak(**bad)
    assert missing in str(exc.value)


def test_upsert_failed_validation_does_not_insert(
    tmp_path: Path, require_fts5: None
) -> None:
    """A validation failure must not leave a row behind."""
    db_path = tmp_path / "recall.db"
    bad = dict(_BASE_ROW)
    bad["title"] = ""
    with RecallStore.open(db_path) as store:
        with pytest.raises(ValueError):
            store.upsert_pak(**bad)
        n = store.conn.execute("SELECT COUNT(*) FROM paks").fetchone()[0]
    assert n == 0


def test_upsert_unknown_superseded_by_raises_integrity(
    tmp_path: Path, require_fts5: None
) -> None:
    """A foreign-key violation on ``superseded_by`` surfaces as IntegrityError."""
    import sqlite3

    db_path = tmp_path / "recall.db"
    bad = dict(_BASE_ROW)
    bad["superseded_by"] = "vault://block/does-not-exist"
    with RecallStore.open(db_path) as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.upsert_pak(**bad)
        n = store.conn.execute("SELECT COUNT(*) FROM paks").fetchone()[0]
    assert n == 0
