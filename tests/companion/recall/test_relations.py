# SPDX-License-Identifier: Apache-2.0
"""``RecallStore`` Pak relation surface."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tokenpak.companion.recall import (
    PAK_RELATION_TYPES,
    PakRelationEntry,
    RecallStore,
)

_BASE_ROW = {
    "pak_id": "vault://block/base",
    "pak_type": "vault",
    "source_type": "doc",
    "authority": "llm_generated",
    "title": "base",
    "content_hash": "0123456789abcdef" * 4,
    "summary": "Base Pak.",
    "project": "tokenpak",
    "topic": "recall",
}


def _seed_pak(store: RecallStore, pak_id: str) -> str:
    row = dict(_BASE_ROW)
    row["pak_id"] = pak_id
    row["title"] = pak_id.rsplit("/", 1)[-1]
    row["content_hash"] = (pak_id.encode("utf-8").hex() * 4)[:64]
    store.upsert_pak(**row)
    return pak_id


def test_set_and_get_relations_round_trip_sorted(
    tmp_path: Path, require_fts5: None
) -> None:
    db_path = tmp_path / "recall.db"
    with RecallStore.open(db_path) as store:
        src = _seed_pak(store, "vault://block/src")
        older = _seed_pak(store, "vault://block/older")
        peer = _seed_pak(store, "vault://block/peer")
        store.set_pak_relations(
            src,
            [
                PakRelationEntry(related_pak_id=older, relation_type="supersedes"),
                PakRelationEntry(related_pak_id=peer, relation_type="conflicts_with"),
            ],
        )
        got = store.get_pak_relations(src)

    assert got == [
        PakRelationEntry(related_pak_id=peer, relation_type="conflicts_with"),
        PakRelationEntry(related_pak_id=older, relation_type="supersedes"),
    ]


def test_relation_type_constants_match_store_validation() -> None:
    assert PAK_RELATION_TYPES == {"supersedes", "conflicts_with"}


def test_set_relations_replaces_prior_set(tmp_path: Path, require_fts5: None) -> None:
    db_path = tmp_path / "recall.db"
    with RecallStore.open(db_path) as store:
        src = _seed_pak(store, "vault://block/src")
        older = _seed_pak(store, "vault://block/older")
        peer = _seed_pak(store, "vault://block/peer")
        store.set_pak_relations(
            src,
            [PakRelationEntry(related_pak_id=older, relation_type="supersedes")],
        )
        store.set_pak_relations(
            src,
            [PakRelationEntry(related_pak_id=peer, relation_type="conflicts_with")],
        )
        got = store.get_pak_relations(src)
        old = store.get_pak(older)

    assert got == [PakRelationEntry(related_pak_id=peer, relation_type="conflicts_with")]
    assert old is not None
    assert old.superseded_by is None


def test_single_supersedes_edge_sets_projection(
    tmp_path: Path, require_fts5: None
) -> None:
    db_path = tmp_path / "recall.db"
    with RecallStore.open(db_path) as store:
        successor = _seed_pak(store, "vault://block/successor")
        older = _seed_pak(store, "vault://block/older")
        store.set_pak_relations(
            successor,
            [PakRelationEntry(related_pak_id=older, relation_type="supersedes")],
        )
        row = store.get_pak(older)

    assert row is not None
    assert row.superseded_by == successor
    assert row.status == "superseded"


def test_multiple_superseders_clear_single_value_projection(
    tmp_path: Path, require_fts5: None
) -> None:
    db_path = tmp_path / "recall.db"
    with RecallStore.open(db_path) as store:
        old = _seed_pak(store, "vault://block/old")
        left = _seed_pak(store, "vault://block/left")
        right = _seed_pak(store, "vault://block/right")
        store.set_pak_relations(
            left,
            [PakRelationEntry(related_pak_id=old, relation_type="supersedes")],
        )
        store.set_pak_relations(
            right,
            [PakRelationEntry(related_pak_id=old, relation_type="supersedes")],
        )
        row = store.get_pak(old)
        incoming = store.conn.execute(
            "SELECT pak_id FROM pak_relations "
            "WHERE related_pak_id = ? AND relation_type = 'supersedes' "
            "ORDER BY pak_id",
            (old,),
        ).fetchall()

    assert row is not None
    assert row.superseded_by is None
    assert [r[0] for r in incoming] == [left, right]


def test_conflict_relation_does_not_derive_conflicted_status(
    tmp_path: Path, require_fts5: None
) -> None:
    db_path = tmp_path / "recall.db"
    with RecallStore.open(db_path) as store:
        src = _seed_pak(store, "vault://block/src")
        peer = _seed_pak(store, "vault://block/peer")
        store.set_pak_relations(
            src,
            [PakRelationEntry(related_pak_id=peer, relation_type="conflicts_with")],
        )
        row = store.get_pak(src)
        peer_row = store.get_pak(peer)

    assert row is not None
    assert peer_row is not None
    assert row.status is None
    assert peer_row.status is None


def test_set_relations_rejects_invalid_input(
    tmp_path: Path, require_fts5: None
) -> None:
    db_path = tmp_path / "recall.db"
    with RecallStore.open(db_path) as store:
        src = _seed_pak(store, "vault://block/src")
        peer = _seed_pak(store, "vault://block/peer")
        with pytest.raises(ValueError, match="relation_type"):
            store.set_pak_relations(
                src,
                [PakRelationEntry(related_pak_id=peer, relation_type="depends_on")],
            )
        with pytest.raises(ValueError, match="duplicate"):
            store.set_pak_relations(
                src,
                [
                    PakRelationEntry(related_pak_id=peer, relation_type="supersedes"),
                    PakRelationEntry(related_pak_id=peer, relation_type="supersedes"),
                ],
            )


def test_set_relations_unknown_pak_raises_integrity_error(
    tmp_path: Path, require_fts5: None
) -> None:
    db_path = tmp_path / "recall.db"
    with RecallStore.open(db_path) as store:
        peer = _seed_pak(store, "vault://block/peer")
        with pytest.raises(sqlite3.IntegrityError):
            store.set_pak_relations(
                "vault://block/missing",
                [PakRelationEntry(related_pak_id=peer, relation_type="supersedes")],
            )
        with pytest.raises(sqlite3.IntegrityError):
            store.set_pak_relations(
                peer,
                [
                    PakRelationEntry(
                        related_pak_id="vault://block/missing",
                        relation_type="supersedes",
                    )
                ],
            )


def test_get_relations_blank_or_unknown_returns_empty(
    tmp_path: Path, require_fts5: None
) -> None:
    db_path = tmp_path / "recall.db"
    with RecallStore.open(db_path) as store:
        _seed_pak(store, "vault://block/src")
        assert store.get_pak_relations(" ") == []
        assert store.get_pak_relations("vault://block/missing") == []
