# SPDX-License-Identifier: Apache-2.0
"""OSS context-selection (apply) proof + R5 recall-boundary coverage.

These tests pin the OSS apply workflow and the Std 32 D5 boundary (Sue R5,
2026-06-29):

- Ranked recall RETRIEVAL is confined to the vault FILE index and is NOT
  served from :class:`RecallStore`. The boundary tests below assert the store
  exposes no Pak-store ranked-recall surface (``recall_preview`` /
  ``_candidate_from_row``) — OSS must not ship rankable retrieval over the
  recall / Pak store (``paks`` / ``paks_fts`` / ``pak_relations``), which is
  Pro-reserved. The OSS recall preview lives in
  :func:`tokenpak.vault.pak_adapter.recall_preview_candidates` (covered in
  ``tests/tip/test_vault_pak_adapter.py``).
- :meth:`RecallStore.build_context_selection` partitions vault-index candidates
  into an explicit include/drop proof — the object Receipt v1 consumes. It is a
  pure, store-agnostic static helper: it reads no ``paks`` rows and needs no
  store instance.
- :meth:`RecallStore.write_context_selection` persists that proof as an
  inspectable, reversible JSON artifact (a pure filesystem write).

Provenance is marked ``oss_vault_file_index`` — the candidates are ranked over
the vault file index, never the recall / Pak store.
"""

from __future__ import annotations

import json
from pathlib import Path

from tokenpak.companion.recall.store import RecallStore


def _cand(pak_id: str, *, rank: int = 1, score: float = 1.0, **over) -> dict:
    """A vault-index-shaped recall candidate dict (see ``recall_preview_candidates``).

    Built directly — the apply proof shaper is store-agnostic, so these tests
    never open a recall db.
    """
    c = {
        "pak_id": pak_id,
        "title": f"title-{pak_id}",
        "snippet": f"snippet-{pak_id}",
        "source": {
            "source_type": "file",
            "authority": "file_source",
            "pak_type": "vault",
            "project": "proj",
        },
        "rank": rank,
        "score": score,
        "reason_codes": [],
        "risk_flags": [],
        "risk": None,
        "status": "proposed",
    }
    c.update(over)
    return c


# ---------------------------------------------------------------------------
# Boundary (Std 32 D5 / R5): OSS ships no Pak-store ranked recall
# ---------------------------------------------------------------------------


def test_recall_store_exposes_no_pak_store_ranked_recall() -> None:
    """R5 regression-lock: the Pak-store recall surface must not exist.

    ``recall_preview`` ranked retrieval over ``paks`` was rejected under
    Std 32 D5; OSS ranked retrieval is confined to the vault file index.
    """
    assert not hasattr(RecallStore, "recall_preview")
    assert not hasattr(RecallStore, "_candidate_from_row")


def test_selection_shaper_is_store_agnostic_static() -> None:
    """The proof shaper needs no store instance (apply never opens recall.db)."""
    sel = RecallStore.build_context_selection([_cand("vault:a")], now="t0")
    assert [c["pak_id"] for c in sel["included"]] == ["vault:a"]
    # Callable straight off the class — no RecallStore(...) instance.
    assert callable(RecallStore.build_context_selection)
    assert callable(RecallStore.write_context_selection)


# ---------------------------------------------------------------------------
# build_context_selection — AC#2 (apply), AC#3 (exclude), AC#4 (proof)
# ---------------------------------------------------------------------------


def test_apply_include_only_drops_the_rest() -> None:
    cands = [_cand("vault:a", rank=1), _cand("vault:b", rank=2), _cand("vault:c", rank=3)]
    sel = RecallStore.build_context_selection(
        cands, include=["vault:a", "vault:b"], now="2026-06-26T00:00:00Z"
    )

    assert sorted(c["pak_id"] for c in sel["included"]) == ["vault:a", "vault:b"]
    assert [c["pak_id"] for c in sel["dropped"]] == ["vault:c"]
    assert sel["dropped"][0]["drop_reason"] == "not_selected"
    assert all(c["decision"] == "included" for c in sel["included"])


def test_apply_exclude_drops_named_keeps_rest() -> None:
    cands = [_cand("vault:a", rank=1), _cand("vault:b", rank=2), _cand("vault:c", rank=3)]
    sel = RecallStore.build_context_selection(
        cands, exclude=["vault:c"], now="2026-06-26T00:00:00Z"
    )

    assert sorted(c["pak_id"] for c in sel["included"]) == ["vault:a", "vault:b"]
    assert [c["pak_id"] for c in sel["dropped"]] == ["vault:c"]
    assert sel["dropped"][0]["drop_reason"] == "user_excluded"


def test_apply_default_includes_everything() -> None:
    cands = [_cand("vault:a", rank=1), _cand("vault:b", rank=2)]
    sel = RecallStore.build_context_selection(cands, now="2026-06-26T00:00:00Z")

    assert sorted(c["pak_id"] for c in sel["included"]) == ["vault:a", "vault:b"]
    assert sel["dropped"] == []


def test_apply_reports_unknown_ids_rather_than_silently_dropping() -> None:
    sel = RecallStore.build_context_selection(
        [_cand("vault:a")], include=["vault:a", "typo-id"], now="2026-06-26T00:00:00Z"
    )

    assert sel["unknown_ids"] == ["typo-id"]
    assert sel["counts"] == {
        "candidates": 1, "included": 1, "dropped": 0, "unknown": 1,
    }


def test_selection_proof_structure_and_provenance() -> None:
    sel = RecallStore.build_context_selection(
        [_cand("vault:a")], query="a", reason="why", now="2026-06-26T00:00:00Z"
    )

    assert sel["schema"] == "tokenpak.recall.context_selection/v1"
    # Candidates are ranked over the vault file index — never the Pak store.
    assert sel["boundary"] == "oss_vault_file_index"
    assert sel["source"] == "oss_vault_file_index"
    # No paks-store path leaks into the proof.
    assert "store" not in sel
    assert sel["query"] == "a"
    assert sel["reason"] == "why"
    assert sel["generated_at"] == "2026-06-26T00:00:00Z"
    assert sel["selection_id"].startswith("sel-")


def test_selection_id_is_deterministic_for_same_partition() -> None:
    cands = [_cand("vault:a", rank=1), _cand("vault:b", rank=2)]
    s1 = RecallStore.build_context_selection(cands, exclude=["vault:b"], query="q", now="t1")
    s2 = RecallStore.build_context_selection(cands, exclude=["vault:b"], query="q", now="t2")
    s3 = RecallStore.build_context_selection(cands, exclude=["vault:a"], query="q", now="t1")

    # Same query + same include/drop partition → stable id (timestamp excluded).
    assert s1["selection_id"] == s2["selection_id"]
    # Different partition → different id.
    assert s1["selection_id"] != s3["selection_id"]


# ---------------------------------------------------------------------------
# write_context_selection — inspectable + reversible artifact
# ---------------------------------------------------------------------------


def test_write_selection_custom_path(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "proof.json"
    sel = RecallStore.build_context_selection([_cand("vault:a")], now="2026-06-26T00:00:00Z")
    out = RecallStore.write_context_selection(sel, path=target)

    assert out == target
    assert json.loads(target.read_text(encoding="utf-8"))["schema"] == sel["schema"]


def test_write_selection_base_dir_is_reloadable(tmp_path: Path) -> None:
    base = tmp_path / "selections"
    sel = RecallStore.build_context_selection([_cand("vault:a")], now="2026-06-26T00:00:00Z")
    out = RecallStore.write_context_selection(sel, base_dir=base)

    assert out.parent == base
    assert out.exists()
    assert json.loads(out.read_text(encoding="utf-8")) == sel  # round-trips losslessly


def test_write_selection_default_path_is_companion_home(
    tmp_path: Path, monkeypatch
) -> None:
    # Default write target is the canonical companion selections/ home — never
    # next to a recall db.
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path))
    sel = RecallStore.build_context_selection([_cand("vault:a")], now="2026-06-26T00:00:00Z")
    out = RecallStore.write_context_selection(sel)

    assert out.parent == tmp_path / "companion" / "selections"
    assert out.exists()


# ---------------------------------------------------------------------------
# AC#5 — boundary: the proof carries only vault-index candidates
# ---------------------------------------------------------------------------


def test_proof_references_only_supplied_candidates() -> None:
    cands = [_cand("vault:a", rank=1), _cand("vault:b", rank=2)]
    sel = RecallStore.build_context_selection(
        cands, exclude=["vault:b"], now="2026-06-26T00:00:00Z"
    )

    supplied_ids = {"vault:a", "vault:b"}
    proof_ids = {c["pak_id"] for c in sel["included"]} | {
        c["pak_id"] for c in sel["dropped"]
    }
    assert proof_ids <= supplied_ids
    # Provenance names the vault-file-index boundary explicitly.
    assert sel["boundary"] == "oss_vault_file_index"
