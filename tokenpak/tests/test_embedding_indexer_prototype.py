"""V4 prototype smoke test for EmbeddingIndexer.

Runs only when ``TOKENPAK_TIP_HYBRID_ENABLED=1`` to keep CI fast (skip
sentence-transformers downloads + embedding compute by default).

Initiative: 2026-04-29-tip-cache-v2-semantic / TCV2 prototype.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest


def _have_vault_and_st() -> bool:
    """Skip if local vault is missing OR sentence-transformers unavailable."""
    if os.environ.get("TOKENPAK_TIP_HYBRID_ENABLED", "").lower() not in ("1", "true", "yes"):
        return False
    if not Path.home().joinpath("vault/.tokenpak/index.json").exists():
        return False
    try:
        import sentence_transformers  # noqa: F401
        import numpy  # noqa: F401
    except Exception:
        return False
    return True


@pytest.mark.skipif(not _have_vault_and_st(), reason="vault index or vector backend unavailable; gated by TOKENPAK_TIP_HYBRID_ENABLED=1")
def test_build_against_subset_corpus():
    """Build embeddings against a tiny subset and confirm search works.

    Uses a tempdir copy of 5 blocks from the real vault index so the
    build cost is ~3-5 seconds and the test is deterministic.
    """
    from tokenpak.retrieval.embedding_indexer import EmbeddingIndexer

    vault_root = Path.home() / "vault" / ".tokenpak"
    src_index = vault_root / "index.json"
    src_blocks = vault_root / "blocks"

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # Build a 5-block subset index
        import json
        full = json.loads(src_index.read_text())
        all_blocks = list(full.get("blocks", {}).items())
        subset = dict(all_blocks[:5])
        (tmp / "index.json").write_text(json.dumps({"version": "1.0", "meta": {}, "blocks": subset}))
        (tmp / "blocks").mkdir()
        copied = 0
        for block_id in subset:
            src_file = src_blocks / f"{block_id}.txt"
            if src_file.exists():
                shutil.copy(src_file, tmp / "blocks" / f"{block_id}.txt")
                copied += 1
        assert copied >= 1, "no blocks copied — vault index doesn't have content files for subset"

        idx = EmbeddingIndexer.from_vault_dir(tmp)
        stats = idx.build_if_stale(force=True)

        assert stats["built"] is True, f"build did not run: {stats}"
        assert stats["blocks"] == copied
        assert stats["dim"] >= 128, f"unexpectedly small embedding dim: {stats}"

        # Idempotent: second build sees fresh hash, skips
        stats2 = idx.build_if_stale()
        assert stats2["built"] is False
        assert stats2["reason"] == "fresh"

        # Search returns hits
        hits = idx.search("the quick brown fox", top_k=3)
        assert len(hits) > 0
        for h in hits:
            assert h.block_id
            assert isinstance(h.score, float)
            assert h.content


@pytest.mark.skipif(not _have_vault_and_st(), reason="gated")
def test_search_returns_relevant_block_for_specific_query():
    """Sanity check: a query about a known topic returns a block whose
    content actually contains related terms."""
    from tokenpak.retrieval.embedding_indexer import EmbeddingIndexer

    # Build against full corpus only if a fresh embeddings dir has been
    # built externally; otherwise build here against ~50 blocks for speed.
    vault_root = Path.home() / "vault" / ".tokenpak"

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        import json
        full = json.loads((vault_root / "index.json").read_text())
        # Take 50 blocks for richer recall
        all_blocks = list(full.get("blocks", {}).items())[:50]
        (tmp / "index.json").write_text(json.dumps({
            "version": "1.0", "meta": {}, "blocks": dict(all_blocks),
        }))
        (tmp / "blocks").mkdir()
        for block_id, _ in all_blocks:
            src_file = vault_root / "blocks" / f"{block_id}.txt"
            if src_file.exists():
                shutil.copy(src_file, tmp / "blocks" / f"{block_id}.txt")

        idx = EmbeddingIndexer.from_vault_dir(tmp)
        idx.build_if_stale(force=True)

        # Issue a query and confirm at least one hit's content contains
        # at least one query term (BM25 would also do this; this test
        # is a sanity floor, not a recall benchmark).
        query = "configuration"
        hits = idx.search(query, top_k=3)
        assert len(hits) >= 1
        any_relevant = any(query.lower() in h.content.lower() or "config" in h.content.lower() for h in hits)
        assert any_relevant, f"no hit's content references the query topic — hits={[h.block_id for h in hits]}"
