"""Regression coverage for the pakplan recall-store drift cleanup.

The pakplan preview/report path historically read its Pak metadata from a
stale ``journal.db`` and joined against the old ``pak_reasons`` / ``pak_risks``
tables — names that predate the canonical recall store. The recall store now
owns ``recall.db`` with ``pak_reason_codes`` and ``pak_risk_flags``.

These tests pin the current names so the drift cannot silently return:

1. ``_recall_db()`` resolves to ``recall.db`` (not ``journal.db``).
2. ``_query_paks()`` reads reason/risk metadata from the canonical
   ``pak_reason_codes`` / ``pak_risk_flags`` tables of a real recall store.
3. The module source carries no stale ``journal.db`` / ``pak_reasons`` /
   ``pak_risks`` literals (mirrors the packet's regression-acceptance grep).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

from tokenpak.cli.commands import pakplan
from tokenpak.companion.recall.store import (
    ReasonCodeEntry,
    RecallStore,
    RiskFlagEntry,
)


def _seed_recall_db(path: Path) -> None:
    store = RecallStore.open(path)
    try:
        store.upsert_pak(
            pak_id="vault://block/example",
            pak_type="vault",
            source_type="doc",
            authority="test",
            title="Example Pak",
            content_hash="deadbeef",
            summary="example summary",
        )
        store.set_pak_reason_codes(
            "vault://block/example",
            [ReasonCodeEntry("current_task", 0.9)],
        )
        store.set_pak_risk_flags(
            "vault://block/example",
            [RiskFlagEntry("mandatory_context_missing", "warn")],
        )
    finally:
        store.close()


def test_recall_db_resolves_to_recall_not_journal() -> None:
    db = pakplan._recall_db()
    assert db is not None
    assert db.name == "recall.db"
    assert "journal.db" not in str(db)


def test_query_paks_reads_canonical_reason_and_risk_tables(tmp_path: Path) -> None:
    db = tmp_path / "recall.db"
    _seed_recall_db(db)

    rows = pakplan._query_paks(db, limit=10)

    assert len(rows) == 1
    row = rows[0]
    assert row["pak_id"] == "vault://block/example"
    # Joined from pak_reason_codes / pak_risk_flags — not the old tables.
    assert row["_reason_codes"] == ["current_task"]
    assert row["_risk_flags"] == ["mandatory_context_missing"]


def test_query_pak_by_id_uses_canonical_store(tmp_path: Path) -> None:
    db = tmp_path / "recall.db"
    _seed_recall_db(db)

    row = pakplan._query_pak_by_id(db, "vault://block/example")

    assert row is not None
    assert row["_reason_codes"] == ["current_task"]
    assert row["_risk_flags"] == ["mandatory_context_missing"]


def test_source_has_no_stale_drift_literals() -> None:
    src = Path(pakplan.__file__).read_text(encoding="utf-8")
    # The recall db source and join tables must use the current names.
    assert "journal.db" not in src
    assert not re.search(r'"pak_reasons"', src)
    assert not re.search(r'"pak_risks"', src)
    assert '"pak_reason_codes"' in src
    assert '"pak_risk_flags"' in src
    assert '"recall.db"' in src


# ---------------------------------------------------------------------------
# recall preview + apply CLI surface (OSS baseline same-store)
# ---------------------------------------------------------------------------


def _seed_two(path: Path) -> None:
    store = RecallStore.open(path)
    try:
        store.upsert_pak(
            pak_id="vault://a", pak_type="vault", source_type="doc",
            authority="file_source", title="Alpha auth design",
            content_hash="aaaa1111", summary="how auth tokens rotate",
            project="proj", now="2026-06-20T10:00:00Z",
        )
        store.upsert_pak(
            pak_id="vault://c", pak_type="vault", source_type="doc",
            authority="file_source", title="Gamma auth runbook",
            content_hash="cccc2222", summary="auth incident escalation",
            project="proj", now="2026-06-22T10:00:00Z",
        )
        store.set_pak_reason_codes("vault://a", [ReasonCodeEntry("current_task", 0.9)])
        store.set_pak_risk_flags(
            "vault://c", [RiskFlagEntry("mandatory_context_missing", "warn")]
        )
    finally:
        store.close()


def test_cli_recall_preview_json_is_unscored_same_store(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    db = tmp_path / "recall.db"
    _seed_two(db)
    monkeypatch.setattr(pakplan, "_recall_db", lambda: db)

    args = SimpleNamespace(
        query="auth", project=None, pak_type=None, limit=10, as_json=True
    )
    rc = pakplan.cmd_pakplan_recall(args)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["boundary"] == "oss_baseline_same_store"
    assert payload["count"] == 2
    # Newest-first, deterministic rank, and unscored (score is null).
    assert [c["pak_id"] for c in payload["candidates"]] == ["vault://c", "vault://a"]
    assert all(c["score"] is None for c in payload["candidates"])
    assert payload["candidates"][0]["risk"] == "warn"


def test_cli_apply_exclude_writes_include_drop_proof(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    db = tmp_path / "recall.db"
    _seed_two(db)
    monkeypatch.setattr(pakplan, "_recall_db", lambda: db)

    out = tmp_path / "proof.json"
    args = SimpleNamespace(
        query="auth", project=None, pak_type=None, limit=10,
        include=None, exclude=["vault://c"], reason="cli demo",
        out=str(out), dry_run=False, as_json=True,
    )
    rc = pakplan.cmd_pakplan_apply(args)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert [c["pak_id"] for c in payload["included"]] == ["vault://a"]
    assert [c["pak_id"] for c in payload["dropped"]] == ["vault://c"]
    assert payload["dropped"][0]["drop_reason"] == "user_excluded"
    assert payload["proof_path"] == str(out)
    # Proof persisted and reloadable for a later Receipt path.
    assert out.exists()
    assert json.loads(out.read_text())["schema"] == "tokenpak.recall.context_selection/v1"


def test_cli_apply_dry_run_does_not_write(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    db = tmp_path / "recall.db"
    _seed_two(db)
    monkeypatch.setattr(pakplan, "_recall_db", lambda: db)

    args = SimpleNamespace(
        query="auth", project=None, pak_type=None, limit=10,
        include=["vault://a"], exclude=None, reason=None,
        out=None, dry_run=True, as_json=True,
    )
    rc = pakplan.cmd_pakplan_apply(args)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["dry_run"] is True
    assert payload["proof_path"] is None
    assert not (db.parent / "selections").exists()


def test_cli_recall_no_db_is_honest(tmp_path: Path, monkeypatch, capsys) -> None:
    missing = tmp_path / "nope.db"
    monkeypatch.setattr(pakplan, "_recall_db", lambda: missing)

    args = SimpleNamespace(
        query=None, project=None, pak_type=None, limit=10, as_json=True
    )
    rc = pakplan.cmd_pakplan_recall(args)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["present"] is False
    assert payload["count"] == 0
    assert payload["candidates"] == []
