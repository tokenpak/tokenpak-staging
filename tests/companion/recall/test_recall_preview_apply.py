# SPDX-License-Identifier: Apache-2.0
"""``RecallStore`` recall preview + apply — OSS baseline same-store coverage.

These tests pin the OSS Recall Preview + Apply workflow:

- :meth:`RecallStore.recall_preview` is single-source, deterministic
  (newest-first) metadata retrieval over *this* store, optionally narrowed by
  byte-literal ``project`` / ``pak_type`` and a case-insensitive substring
  ``query``. It is **unscored** — ``score`` is always ``None`` and ``rank`` is
  the position in the deterministic order, never a learned relevance score.
- :meth:`RecallStore.build_context_selection` partitions candidates into an
  explicit include/drop proof — the object Receipt v1 consumes as its context
  proof.
- :meth:`RecallStore.write_context_selection` persists that proof as an
  inspectable, reversible JSON artifact.

The boundary tests assert the workflow stays inside OSS baseline same-store
recall: the proof references only pak_ids from the single store it was built
against, and carries an honest ``oss_baseline_same_store`` provenance marker.
No Pro cross-source ranking / capture / hydration is exercised.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tokenpak.companion.recall.store import (
    ReasonCodeEntry,
    RecallStore,
    RiskFlagEntry,
)


def _seed(store: RecallStore, *rows: dict[str, Any]) -> None:
    for r in rows:
        store.upsert_pak(**r)


def _row(
    pak_id: str,
    *,
    now: str,
    title: str | None = None,
    summary: str = "",
    project: str | None = "proj",
    pak_type: str = "vault",
    source_type: str = "doc",
    authority: str = "file_source",
) -> dict[str, Any]:
    return {
        "pak_id": pak_id,
        "pak_type": pak_type,
        "source_type": source_type,
        "authority": authority,
        "title": title or f"title-{pak_id}",
        "content_hash": (pak_id * 8)[:32],
        "summary": summary,
        "project": project,
        "now": now,
    }


# ---------------------------------------------------------------------------
# recall_preview — AC#1 (source, score/rank, snippet/title, risk/reason)
# ---------------------------------------------------------------------------


def test_recall_preview_empty_store(tmp_path: Path, require_fts5: None) -> None:
    with RecallStore.open(tmp_path / "recall.db") as store:
        assert store.recall_preview() == []
        assert store.recall_preview(query="anything") == []


def test_recall_preview_newest_first_unscored(
    tmp_path: Path, require_fts5: None
) -> None:
    with RecallStore.open(tmp_path / "recall.db") as store:
        _seed(
            store,
            _row("a", now="2026-06-20T10:00:00Z"),
            _row("b", now="2026-06-22T10:00:00Z"),
            _row("c", now="2026-06-21T10:00:00Z"),
        )
        out = store.recall_preview()

    assert [c["pak_id"] for c in out] == ["b", "c", "a"]
    # Deterministic position, never a learned score.
    assert [c["rank"] for c in out] == [1, 2, 3]
    assert all(c["score"] is None for c in out)


def test_recall_preview_candidate_shape(tmp_path: Path, require_fts5: None) -> None:
    with RecallStore.open(tmp_path / "recall.db") as store:
        _seed(
            store,
            _row("a", now="2026-06-20T10:00:00Z", title="Auth design",
                 summary="how tokens rotate"),
        )
        store.set_pak_reason_codes("a", [ReasonCodeEntry("current_task", 0.9)])
        out = store.recall_preview()

    c = out[0]
    assert set(c) >= {
        "pak_id", "title", "snippet", "source", "rank", "score",
        "reason_codes", "risk_flags", "risk",
    }
    assert c["source"] == {
        "source_type": "doc",
        "authority": "file_source",
        "pak_type": "vault",
        "project": "proj",
    }
    assert c["snippet"] == "how tokens rotate"
    assert c["reason_codes"] == ["current_task"]


def test_recall_preview_query_is_case_insensitive_substring(
    tmp_path: Path, require_fts5: None
) -> None:
    with RecallStore.open(tmp_path / "recall.db") as store:
        _seed(
            store,
            _row("a", now="2026-06-20T10:00:00Z", title="Alpha AUTH design",
                 summary="gateway"),
            _row("b", now="2026-06-21T10:00:00Z", title="Beta billing",
                 summary="reconciliation across providers"),
            _row("c", now="2026-06-22T10:00:00Z", title="Gamma runbook",
                 summary="auth incident escalation"),
        )
        # Matches title (a, case-insensitively) and summary (c) but not b.
        out = store.recall_preview(query="auth")

    assert sorted(c["pak_id"] for c in out) == ["a", "c"]


def test_recall_preview_byte_literal_filters(
    tmp_path: Path, require_fts5: None
) -> None:
    with RecallStore.open(tmp_path / "recall.db") as store:
        _seed(
            store,
            _row("a", now="2026-06-20T10:00:00Z", project="alpha", pak_type="vault"),
            _row("b", now="2026-06-21T10:00:00Z", project="beta", pak_type="vault"),
            _row("c", now="2026-06-22T10:00:00Z", project="alpha",
                 pak_type="interaction"),
        )
        by_project = store.recall_preview(project="alpha")
        by_type = store.recall_preview(pak_type="interaction")
        composed = store.recall_preview(project="alpha", pak_type="vault")

    assert sorted(c["pak_id"] for c in by_project) == ["a", "c"]
    assert [c["pak_id"] for c in by_type] == ["c"]
    assert [c["pak_id"] for c in composed] == ["a"]


def test_recall_preview_limit_bounds_results(
    tmp_path: Path, require_fts5: None
) -> None:
    with RecallStore.open(tmp_path / "recall.db") as store:
        _seed(store, *[
            _row(f"p{i:02d}", now=f"2026-06-{10 + i:02d}T10:00:00Z")
            for i in range(8)
        ])
        out = store.recall_preview(limit=3)

    assert len(out) == 3
    assert [c["rank"] for c in out] == [1, 2, 3]


def test_recall_preview_surfaces_top_risk_severity(
    tmp_path: Path, require_fts5: None
) -> None:
    with RecallStore.open(tmp_path / "recall.db") as store:
        _seed(store, _row("a", now="2026-06-20T10:00:00Z"))
        store.set_pak_risk_flags("a", [
            RiskFlagEntry("stale_content", "info"),
            RiskFlagEntry("mandatory_context_missing", "warn"),
            RiskFlagEntry("conflicting_guidance", "block"),
        ])
        c = store.recall_preview()[0]

    # block > warn > info — the most severe flag is surfaced as ``risk``.
    assert c["risk"] == "block"
    assert {f["severity"] for f in c["risk_flags"]} == {"info", "warn", "block"}


# ---------------------------------------------------------------------------
# build_context_selection — AC#2 (apply), AC#3 (exclude), AC#4 (proof)
# ---------------------------------------------------------------------------


def _candidates(store: RecallStore) -> list[dict]:
    return store.recall_preview()


def test_apply_include_only_drops_the_rest(
    tmp_path: Path, require_fts5: None
) -> None:
    with RecallStore.open(tmp_path / "recall.db") as store:
        _seed(
            store,
            _row("a", now="2026-06-20T10:00:00Z"),
            _row("b", now="2026-06-21T10:00:00Z"),
            _row("c", now="2026-06-22T10:00:00Z"),
        )
        sel = store.build_context_selection(
            _candidates(store), include=["a", "b"], now="2026-06-26T00:00:00Z"
        )

    assert sorted(c["pak_id"] for c in sel["included"]) == ["a", "b"]
    assert [c["pak_id"] for c in sel["dropped"]] == ["c"]
    assert sel["dropped"][0]["drop_reason"] == "not_selected"
    assert all(c["decision"] == "included" for c in sel["included"])


def test_apply_exclude_drops_named_keeps_rest(
    tmp_path: Path, require_fts5: None
) -> None:
    with RecallStore.open(tmp_path / "recall.db") as store:
        _seed(
            store,
            _row("a", now="2026-06-20T10:00:00Z"),
            _row("b", now="2026-06-21T10:00:00Z"),
            _row("c", now="2026-06-22T10:00:00Z"),
        )
        sel = store.build_context_selection(
            _candidates(store), exclude=["c"], now="2026-06-26T00:00:00Z"
        )

    assert sorted(c["pak_id"] for c in sel["included"]) == ["a", "b"]
    assert [c["pak_id"] for c in sel["dropped"]] == ["c"]
    assert sel["dropped"][0]["drop_reason"] == "user_excluded"


def test_apply_default_includes_everything(
    tmp_path: Path, require_fts5: None
) -> None:
    with RecallStore.open(tmp_path / "recall.db") as store:
        _seed(
            store,
            _row("a", now="2026-06-20T10:00:00Z"),
            _row("b", now="2026-06-21T10:00:00Z"),
        )
        sel = store.build_context_selection(
            _candidates(store), now="2026-06-26T00:00:00Z"
        )

    assert sorted(c["pak_id"] for c in sel["included"]) == ["a", "b"]
    assert sel["dropped"] == []


def test_apply_reports_unknown_ids_rather_than_silently_dropping(
    tmp_path: Path, require_fts5: None
) -> None:
    with RecallStore.open(tmp_path / "recall.db") as store:
        _seed(store, _row("a", now="2026-06-20T10:00:00Z"))
        sel = store.build_context_selection(
            _candidates(store), include=["a", "typo-id"], now="2026-06-26T00:00:00Z"
        )

    assert sel["unknown_ids"] == ["typo-id"]
    assert sel["counts"] == {
        "candidates": 1, "included": 1, "dropped": 0, "unknown": 1,
    }


def test_selection_proof_structure_and_provenance(
    tmp_path: Path, require_fts5: None
) -> None:
    db = tmp_path / "recall.db"
    with RecallStore.open(db) as store:
        _seed(store, _row("a", now="2026-06-20T10:00:00Z"))
        sel = store.build_context_selection(
            _candidates(store), query="a", reason="why", now="2026-06-26T00:00:00Z"
        )

    assert sel["schema"] == "tokenpak.recall.context_selection/v1"
    assert sel["boundary"] == "oss_baseline_same_store"
    assert sel["query"] == "a"
    assert sel["reason"] == "why"
    assert sel["generated_at"] == "2026-06-26T00:00:00Z"
    assert sel["store"] == str(db)
    assert sel["selection_id"].startswith("sel-")


def test_selection_id_is_deterministic_for_same_partition(
    tmp_path: Path, require_fts5: None
) -> None:
    with RecallStore.open(tmp_path / "recall.db") as store:
        _seed(
            store,
            _row("a", now="2026-06-20T10:00:00Z"),
            _row("b", now="2026-06-21T10:00:00Z"),
        )
        cands = _candidates(store)
        s1 = store.build_context_selection(cands, exclude=["b"], query="q", now="t1")
        s2 = store.build_context_selection(cands, exclude=["b"], query="q", now="t2")
        s3 = store.build_context_selection(cands, exclude=["a"], query="q", now="t1")

    # Same query + same include/drop partition → stable id (timestamp excluded).
    assert s1["selection_id"] == s2["selection_id"]
    # Different partition → different id.
    assert s1["selection_id"] != s3["selection_id"]


# ---------------------------------------------------------------------------
# write_context_selection — inspectable + reversible artifact
# ---------------------------------------------------------------------------


def test_write_selection_default_path_is_reloadable(
    tmp_path: Path, require_fts5: None
) -> None:
    db = tmp_path / "recall.db"
    with RecallStore.open(db) as store:
        _seed(store, _row("a", now="2026-06-20T10:00:00Z"))
        sel = store.build_context_selection(
            _candidates(store), now="2026-06-26T00:00:00Z"
        )
        out = store.write_context_selection(sel)

    # Written next to the recall db, under a discoverable selections/ dir.
    assert out.parent == db.parent / "selections"
    assert out.exists()
    reloaded = json.loads(out.read_text(encoding="utf-8"))
    assert reloaded == sel  # round-trips losslessly


def test_write_selection_custom_path(tmp_path: Path, require_fts5: None) -> None:
    db = tmp_path / "recall.db"
    target = tmp_path / "nested" / "proof.json"
    with RecallStore.open(db) as store:
        _seed(store, _row("a", now="2026-06-20T10:00:00Z"))
        sel = store.build_context_selection(
            _candidates(store), now="2026-06-26T00:00:00Z"
        )
        out = store.write_context_selection(sel, path=target)

    assert out == target
    assert json.loads(target.read_text(encoding="utf-8"))["schema"] == sel["schema"]


# ---------------------------------------------------------------------------
# AC#5 — boundary: stays inside OSS baseline same-store recall
# ---------------------------------------------------------------------------


def test_proof_references_only_same_store_paks(
    tmp_path: Path, require_fts5: None
) -> None:
    db = tmp_path / "recall.db"
    with RecallStore.open(db) as store:
        _seed(
            store,
            _row("a", now="2026-06-20T10:00:00Z"),
            _row("b", now="2026-06-21T10:00:00Z"),
        )
        cands = _candidates(store)
        sel = store.build_context_selection(
            cands, exclude=["b"], now="2026-06-26T00:00:00Z"
        )

    store_ids = {"a", "b"}
    proof_ids = {c["pak_id"] for c in sel["included"]} | {
        c["pak_id"] for c in sel["dropped"]
    }
    # Every pak in the proof came from the single store it was built against.
    assert proof_ids <= store_ids
    # Provenance marker names the single-source OSS boundary explicitly.
    assert sel["boundary"] == "oss_baseline_same_store"
    assert sel["store"] == str(db)
