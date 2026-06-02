"""Smoke tests for TIP-cache v2 hybrid wiring (default-OFF gate).

Covers (per packet RATIFIED-REMAINDER-E acceptance criteria):
  * ``TOKENPAK_TIP_HYBRID_ENABLED`` defaults OFF; retrieval unchanged when unset.
  * Embedding loader is LOCAL-ONLY: an uncached model returns None (no egress).
  * Gate-on: ``VaultIndex.search`` delegates to the hybrid retriever and the
    embeddings artifact (embeddings.npy + embeddings.meta.json) is produced and
    loadable; gate-off falls back cleanly to BM25.

Tests that require the sentence-transformers model are skipped when it is not
locally cached, keeping CI fast and egress-free.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tokenpak.retrieval import embedding_model
from tokenpak.retrieval.vault_index import VaultIndex, _hybrid_gate_enabled
from tokenpak.vault.blocks import BlockStore

_MODEL_CACHED = embedding_model.is_model_cached(embedding_model.DEFAULT_MODEL)
_requires_model = pytest.mark.skipif(
    not _MODEL_CACHED,
    reason="default embedding model not in local cache; egress disallowed (Std 49)",
)

_BLOCKS = {
    "doc.compression.md": "TokenPak compresses vault content into blocks to save tokens.",
    "doc.retrieval.md": "BM25 retrieval scores blocks by term frequency and inverse document frequency.",
    "doc.hybrid.md": "Hybrid search fuses BM25 and local vector embeddings via reciprocal rank fusion.",
    "doc.privacy.md": "Embeddings are built locally with no network egress per the privacy standard.",
}


def _write_vault(tmp_path: Path) -> Path:
    """Materialize a minimal .tokenpak index dir (index.json + blocks/*.txt)."""
    tk = tmp_path / ".tokenpak"
    blocks_dir = tk / "blocks"
    blocks_dir.mkdir(parents=True)
    index = {"blocks": {}}
    for bid, content in _BLOCKS.items():
        (blocks_dir / f"{bid}.txt").write_text(content, encoding="utf-8")
        index["blocks"][bid] = {
            "source_path": bid,
            "risk_class": "narrative",
            "must_keep": False,
            "raw_tokens": max(1, len(content) // 4),
        }
    (tk / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return tk


def _docs():
    return [
        {"id": bid, "content": content, "source_path": bid, "raw_tokens": 1}
        for bid, content in _BLOCKS.items()
    ]


def test_gate_defaults_off(monkeypatch):
    monkeypatch.delenv("TOKENPAK_TIP_HYBRID_ENABLED", raising=False)
    assert _hybrid_gate_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "TRUE"])
def test_gate_on_recognized_values(monkeypatch, val):
    monkeypatch.setenv("TOKENPAK_TIP_HYBRID_ENABLED", val)
    assert _hybrid_gate_enabled() is True


def test_load_model_offline_guard_returns_none(monkeypatch):
    """Uncached model must return None (never download / never raise)."""
    assert embedding_model.load_model("not-a-real-model-zzz-0000") is None


def test_resolve_model_id_env_override(monkeypatch):
    monkeypatch.setenv("TOKENPAK_EMBEDDING_MODEL", "custom-model-x")
    assert embedding_model.resolve_model_id() == "custom-model-x"


def test_blockstore_default_off_returns_empty(monkeypatch):
    monkeypatch.delenv("TOKENPAK_TIP_BM25_ENABLED", raising=False)
    monkeypatch.delenv("TOKENPAK_TIP_HYBRID_ENABLED", raising=False)
    BlockStore.reset_default_for_tests()
    try:
        store = BlockStore.default()
        assert len(store) == 0  # default-OFF: no vault retrieval wired
    finally:
        BlockStore.reset_default_for_tests()


def test_vault_index_bm25_when_gate_off(tmp_path, monkeypatch):
    monkeypatch.delenv("TOKENPAK_TIP_HYBRID_ENABLED", raising=False)
    tk = _write_vault(tmp_path)
    vi = VaultIndex(str(tk))
    vi.maybe_reload()
    results = vi.search("retrieval", top_k=3, min_score=0.0)
    assert results, "BM25 should return hits with the gate off"
    block, score = results[0]
    assert "block_id" in block and isinstance(score, float)


@_requires_model
def test_select_local_model_picks_cached(monkeypatch):
    info = embedding_model.select_local_model()
    assert info is not None
    assert info.model_id
    assert info.dim > 0


@_requires_model
def test_embeddings_artifact_built_and_loadable(tmp_path):
    from tokenpak.retrieval.vector_local import LocalVectorRetriever

    tk = _write_vault(tmp_path)
    retriever = LocalVectorRetriever(index_path=str(tk))
    count = asyncio.run(retriever.index(_docs()))
    assert count == len(_BLOCKS)
    assert (tk / "embeddings.npy").exists()
    manifest = tk / "embeddings.meta.json"
    assert manifest.exists()
    meta = json.loads(manifest.read_text())
    assert meta["dim"] > 0 and meta["model_id"] and meta["count"] == len(_BLOCKS)

    # Reload from disk in a fresh retriever — artifact is self-sufficient.
    reloaded = LocalVectorRetriever(index_path=str(tk))
    assert reloaded.load() is True


@_requires_model
def test_vault_index_hybrid_when_gate_on(tmp_path, monkeypatch):
    from tokenpak.retrieval.vector_local import LocalVectorRetriever

    tk = _write_vault(tmp_path)
    asyncio.run(LocalVectorRetriever(index_path=str(tk)).index(_docs()))

    monkeypatch.setenv("TOKENPAK_TIP_HYBRID_ENABLED", "1")
    vi = VaultIndex(str(tk))
    vi.maybe_reload()
    results = vi.search("semantic vector fusion", top_k=3)
    assert results, "hybrid path should return fused results when gate is on"
    block, score = results[0]
    assert "block_id" in block and isinstance(score, float)


@_requires_model
def test_hybrid_falls_back_to_bm25_on_missing_vectors(tmp_path, monkeypatch):
    """Worst case (no embeddings artifact): hybrid degrades to BM25, never errors."""
    tk = _write_vault(tmp_path)  # note: no embeddings.npy built
    monkeypatch.setenv("TOKENPAK_TIP_HYBRID_ENABLED", "1")
    vi = VaultIndex(str(tk))
    vi.maybe_reload()
    results = vi.search("retrieval", top_k=3, min_score=0.0)
    assert results, "BM25 fallback must still return hits"
