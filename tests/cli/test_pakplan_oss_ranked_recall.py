from __future__ import annotations

import argparse
import json
from pathlib import Path

from tokenpak.cli.commands import pakplan
from tokenpak.companion.recall.ranker import rank_paks
from tokenpak.companion.recall.store import (
    ReasonCodeEntry,
    RecallStore,
    RiskFlagEntry,
)


def _seed_ranked_recall_db(path: Path) -> None:
    store = RecallStore.open(path)
    try:
        # Relevant Paks are inserted first so raw rowid-desc preview would put
        # the later distractors ahead of them. Ranked preview must correct that.
        store.upsert_pak(
            pak_id="pak-decision-release",
            pak_type="decision",
            source_type="manual",
            authority="user_approved",
            title="Release identity decision",
            summary="Canonical decision for release gate identity and benchmark claim boundary.",
            content_hash="decision-hash",
            project="tokenpak",
            now="2026-07-01T01:00:00Z",
        )
        store.set_pak_reason_codes(
            "pak-decision-release",
            [
                ReasonCodeEntry("current_task", 1.0),
                ReasonCodeEntry("authoritative_decision", 0.8),
            ],
        )

        store.upsert_pak(
            pak_id="pak-session-benchmark",
            pak_type="interaction",
            source_type="session",
            authority="tool_result",
            title="Session benchmark notes",
            summary="Prior session measured PAK recall benchmark savings and fixed top-k coverage.",
            content_hash="session-hash",
            project="tokenpak",
            now="2026-07-01T01:01:00Z",
        )
        store.set_pak_reason_codes(
            "pak-session-benchmark",
            [
                ReasonCodeEntry("current_task", 1.0),
                ReasonCodeEntry("recent_user_reference", 1.0),
            ],
        )

        store.upsert_pak(
            pak_id="pak-vault-benchmark",
            pak_type="vault",
            source_type="doc",
            authority="file_source",
            title="Benchmark reference",
            summary="Benchmark claim boundary and local evidence requirements.",
            content_hash="vault-hash",
            project="tokenpak",
            now="2026-07-01T01:02:00Z",
        )
        store.set_pak_reason_codes(
            "pak-vault-benchmark",
            [ReasonCodeEntry("current_task", 0.9)],
        )

        for i in range(5):
            pid = f"pak-distractor-{i}"
            store.upsert_pak(
                pak_id=pid,
                pak_type="vault",
                source_type="doc",
                authority="file_source",
                title=f"Newest distractor {i}",
                summary="Unrelated site, billing, or marketing note.",
                content_hash=f"distractor-{i}",
                project="tokenpak",
                now=f"2026-07-01T02:0{i}:00Z",
            )
            store.set_pak_reason_codes(pid, [ReasonCodeEntry("ambient_context", 0.05)])
            store.set_pak_risk_flags(pid, [RiskFlagEntry("scope_expansion", "warn")])
    finally:
        store.close()


def test_rank_paks_includes_decision_and_session_paks() -> None:
    rows = [
        {
            "pak_id": "pak-distractor",
            "pak_type": "vault",
            "authority": "file_source",
            "title": "New unrelated note",
            "_reason_codes": ["ambient_context"],
            "_risk_flag_entries": [{"risk_flag": "scope_expansion", "severity": "warn"}],
        },
        {
            "pak_id": "pak-decision",
            "pak_type": "decision",
            "authority": "user_approved",
            "title": "Accepted benchmark decision",
            "_reason_code_entries": [
                {"reason_code": "current_task", "weight": 1.0},
                {"reason_code": "authoritative_decision", "weight": 0.8},
            ],
        },
        {
            "pak_id": "pak-session",
            "pak_type": "interaction",
            "authority": "tool_result",
            "title": "Session recall benchmark notes",
            "_reason_code_entries": [
                {"reason_code": "current_task", "weight": 1.0},
                {"reason_code": "recent_user_reference", "weight": 1.0},
            ],
        },
    ]

    ranked = rank_paks(rows, query="benchmark decision session", limit=2)

    assert [r["pak_id"] for r in ranked] == ["pak-decision", "pak-session"]
    assert ranked[0]["_oss_score"] > ranked[1]["_oss_score"] > 0
    assert any("reason:authoritative_decision" in r for r in ranked[0]["_oss_score_reasons"])


def test_pakplan_ranked_preview_beats_newest_first_distractors(tmp_path: Path) -> None:
    db = tmp_path / "recall.db"
    _seed_ranked_recall_db(db)

    raw = pakplan._query_paks(db, limit=3)
    ranked = pakplan._query_ranked_paks(
        db,
        query="release identity benchmark claim boundary",
        limit=3,
    )

    assert [r["pak_id"] for r in raw] == [
        "pak-distractor-4",
        "pak-distractor-3",
        "pak-distractor-2",
    ]
    assert [r["pak_id"] for r in ranked] == [
        "pak-decision-release",
        "pak-session-benchmark",
        "pak-vault-benchmark",
    ]
    assert all(r["_oss_score"] > 0 for r in ranked)


def test_pakplan_preview_json_reports_oss_ranked_lite(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db = tmp_path / "recall.db"
    _seed_ranked_recall_db(db)
    monkeypatch.setattr(pakplan, "_recall_db", lambda: db)

    args = argparse.Namespace(
        limit=2,
        query="release benchmark decision",
        as_json=True,
        unranked=False,
    )

    assert pakplan.cmd_pakplan_preview(args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["scoring"] == "oss-ranked-lite"
    assert payload["query"] == "release benchmark decision"
    assert [p["pak_id"] for p in payload["paks"]] == [
        "pak-decision-release",
        "pak-session-benchmark",
    ]
    assert payload["paks"][0]["oss_score"] > 0
