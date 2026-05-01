"""test_retrieval_module.py — Unit tests for tokenpak.retrieval subpackage.

Covers: base (dataclasses, HybridSearchConfig.from_env, Retriever ABC),
        bm25 (BM25Index, BM25Retriever, _tokenize),
        fusion (rrf_fusion, rrf_fusion_detailed, WeightedFusion),
        hybrid (HybridRetriever — vector mocked out),
        vault_index (VaultIndex, _bm25_tokenize),
        vector_local (LocalVectorRetriever — sentence-transformers mocked).

No live API calls or model downloads — all external deps mocked.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch
import tempfile
import pytest


# ---------------------------------------------------------------------------
# ── base.py ─────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

from tokenpak.retrieval.base import (
    FusedResult,
    HybridSearchConfig,
    Retriever,
    RetrievalQuery,
    RetrievalResult,
    RetrieverType,
)


class TestRetrieverType:
    def test_enum_values(self):
        assert RetrieverType.BM25.value == "bm25"
        assert RetrieverType.VECTOR.value == "vector"
        assert RetrieverType.HYBRID.value == "hybrid"

    def test_distinct_members(self):
        assert RetrieverType.BM25 != RetrieverType.VECTOR
        assert RetrieverType.VECTOR != RetrieverType.HYBRID


class TestRetrievalResult:
    def test_default_retriever_type(self):
        r = RetrievalResult(doc_id="d1", score=1.0, content="hello")
        assert r.retriever_type == RetrieverType.BM25

    def test_custom_retriever_type(self):
        r = RetrievalResult(doc_id="d1", score=0.9, content="x", retriever_type=RetrieverType.VECTOR)
        assert r.retriever_type == RetrieverType.VECTOR

    def test_metadata_default_empty(self):
        r = RetrievalResult(doc_id="d1", score=1.0, content="x")
        assert r.metadata == {}

    def test_metadata_stored(self):
        r = RetrievalResult(doc_id="d1", score=1.0, content="x", metadata={"k": "v"})
        assert r.metadata["k"] == "v"

    def test_repr_contains_doc_id(self):
        r = RetrievalResult(doc_id="myid", score=0.5, content="x")
        assert "myid" in repr(r)


class TestRetrievalQuery:
    def test_defaults(self):
        q = RetrievalQuery(text="hello")
        assert q.top_k == 10
        assert q.min_score == 0.0
        assert q.filters == {}

    def test_custom_values(self):
        q = RetrievalQuery(text="foo", top_k=5, min_score=0.3, filters={"tag": "x"})
        assert q.text == "foo"
        assert q.top_k == 5
        assert q.min_score == 0.3
        assert q.filters["tag"] == "x"


class TestFusedResult:
    def _make(self, doc_id: str = "d1", content: str = "hello") -> FusedResult:
        r = RetrievalResult(doc_id=doc_id, score=1.0, content=content, metadata={"src": "bm25"})
        return FusedResult(doc_id=doc_id, fused_score=0.5, source_results={"bm25": r})

    def test_content_from_source(self):
        fr = self._make(content="world")
        assert fr.content == "world"

    def test_content_empty_when_no_sources(self):
        fr = FusedResult(doc_id="x", fused_score=0.5)
        assert fr.content == ""

    def test_metadata_merged(self):
        r1 = RetrievalResult(doc_id="d1", score=1.0, content="a", metadata={"k1": "v1"})
        r2 = RetrievalResult(doc_id="d1", score=0.9, content="b", metadata={"k2": "v2"})
        fr = FusedResult(doc_id="d1", fused_score=0.7, source_results={"s1": r1, "s2": r2})
        assert "k1" in fr.metadata
        assert "k2" in fr.metadata

    def test_repr_contains_doc_id(self):
        fr = self._make("zzzid")
        assert "zzzid" in repr(fr)


class TestHybridSearchConfig:
    def test_defaults(self):
        cfg = HybridSearchConfig()
        assert cfg.bm25_weight == 0.5
        assert cfg.vector_weight == 0.5
        assert cfg.rrf_k == 60
        assert cfg.top_k == 20

    def test_from_env_defaults(self):
        env = {}
        with patch.dict(os.environ, env, clear=False):
            # Remove relevant keys to ensure defaults
            for k in [
                "TOKENPAK_RETRIEVAL_MODE", "TOKENPAK_BM25_WEIGHT",
                "TOKENPAK_VECTOR_WEIGHT", "TOKENPAK_RRF_K", "TOKENPAK_RETRIEVAL_TOP_K",
            ]:
                os.environ.pop(k, None)
            cfg = HybridSearchConfig.from_env()
        assert cfg.bm25_weight == 0.5
        assert cfg.vector_weight == 0.5

    def test_from_env_bm25_mode(self):
        with patch.dict(os.environ, {"TOKENPAK_RETRIEVAL_MODE": "bm25"}, clear=False):
            cfg = HybridSearchConfig.from_env()
        assert cfg.bm25_weight == 1.0
        assert cfg.vector_weight == 0.0

    def test_from_env_vector_mode(self):
        with patch.dict(os.environ, {"TOKENPAK_RETRIEVAL_MODE": "vector"}, clear=False):
            cfg = HybridSearchConfig.from_env()
        assert cfg.bm25_weight == 0.0
        assert cfg.vector_weight == 1.0

    def test_from_env_custom_weights(self):
        env = {
            "TOKENPAK_BM25_WEIGHT": "0.7",
            "TOKENPAK_VECTOR_WEIGHT": "0.3",
            "TOKENPAK_RRF_K": "30",
            "TOKENPAK_RETRIEVAL_TOP_K": "15",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = HybridSearchConfig.from_env()
        assert cfg.bm25_weight == pytest.approx(0.7)
        assert cfg.vector_weight == pytest.approx(0.3)
        assert cfg.rrf_k == 30
        assert cfg.top_k == 15

    def test_from_env_bad_values_use_defaults(self):
        """Invalid env var values should fall back to defaults, not raise."""
        env = {"TOKENPAK_BM25_WEIGHT": "not_a_float", "TOKENPAK_RRF_K": "NaN"}
        with patch.dict(os.environ, env, clear=False):
            cfg = HybridSearchConfig.from_env()
        assert cfg.bm25_weight == 0.5
        assert cfg.rrf_k == 60

    def test_from_env_index_paths(self):
        env = {
            "TOKENPAK_VECTOR_INDEX_PATH": "/tmp/vec",
            "TOKENPAK_VAULT_INDEX_PATH": "/tmp/vault",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = HybridSearchConfig.from_env()
        assert cfg.vector_index_path == "/tmp/vec"
        assert cfg.vault_index_path == "/tmp/vault"


class TestRetrieverABC:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            Retriever()  # type: ignore[abstract]

    def test_concrete_subclass_works(self):
        class ConcreteRetriever(Retriever):
            @property
            def retriever_type(self):
                return RetrieverType.BM25

            async def search(self, query):
                return []

            async def index(self, documents):
                return 0

        r = ConcreteRetriever()
        assert r.retriever_type == RetrieverType.BM25
        assert r.is_available() is True  # base class default


# ---------------------------------------------------------------------------
# ── bm25.py ──────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

from tokenpak.retrieval.bm25 import BM25Index, BM25Retriever, _tokenize


SAMPLE_DOCS = [
    {"id": "doc1", "content": "The quick brown fox jumps over the lazy dog"},
    {"id": "doc2", "content": "Python is a great programming language for data science"},
    {"id": "doc3", "content": "Machine learning algorithms process large datasets efficiently"},
    {"id": "doc4", "content": "The fox ran quickly through the forest"},
    {"id": "doc5", "content": "Data science uses statistical methods and machine learning"},
]


class TestTokenize:
    def test_lowercases(self):
        assert _tokenize("HELLO WORLD") == _tokenize("hello world")

    def test_extracts_alphanumeric(self):
        tokens = _tokenize("foo-bar baz_qux 99")
        assert "foo" in tokens
        assert "bar" in tokens
        assert "baz_qux" in tokens
        assert "99" in tokens

    def test_strips_punctuation(self):
        tokens = _tokenize("hello, world!")
        assert "," not in tokens
        assert "!" not in tokens

    def test_empty_string(self):
        assert _tokenize("") == []

    def test_numbers_kept(self):
        assert "123" in _tokenize("abc 123")


class TestBM25Index:
    def setup_method(self):
        self.idx = BM25Index()
        self.idx.build(SAMPLE_DOCS)

    def test_build_count(self):
        assert self.idx.doc_count == len(SAMPLE_DOCS)

    def test_rebuild_resets(self):
        fresh = BM25Index()
        fresh.build([{"id": "x", "content": "hello"}])
        assert fresh.doc_count == 1
        fresh.build(SAMPLE_DOCS)
        assert fresh.doc_count == len(SAMPLE_DOCS)

    def test_search_returns_results(self):
        results = self.idx.search("fox")
        assert len(results) > 0

    def test_relevant_docs_ranked(self):
        results = self.idx.search("fox", top_k=5)
        ids = {r.doc_id for r in results}
        assert "doc1" in ids or "doc4" in ids

    def test_scores_descending(self):
        results = self.idx.search("data science machine learning", top_k=5)
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_no_match_returns_empty(self):
        assert self.idx.search("xyzzy_nonexistent_qqq") == []

    def test_top_k_respected(self):
        results = self.idx.search("the", top_k=2)
        assert len(results) <= 2

    def test_min_score_filters(self):
        all_r = self.idx.search("fox", top_k=5, min_score=0.0)
        strict = self.idx.search("fox", top_k=5, min_score=9999.0)
        assert len(strict) <= len(all_r)
        for r in strict:
            assert r.score >= 9999.0

    def test_result_fields(self):
        results = self.idx.search("fox", top_k=1)
        r = results[0]
        assert r.doc_id in {"doc1", "doc4"}
        assert r.content != ""
        assert r.retriever_type == RetrieverType.BM25

    def test_empty_index_returns_empty(self):
        idx = BM25Index()
        assert idx.search("fox") == []

    def test_empty_query_returns_empty(self):
        assert self.idx.search("") == []


class TestBM25Retriever:
    def test_retriever_type(self):
        assert BM25Retriever().retriever_type == RetrieverType.BM25

    def test_not_available_initially(self):
        r = BM25Retriever()
        assert not r._loaded
        assert not r.is_available()

    def test_available_after_index(self):
        r = BM25Retriever()
        asyncio.run(r.index(SAMPLE_DOCS))
        assert r.is_available()

    def test_index_returns_count(self):
        r = BM25Retriever()
        count = asyncio.run(r.index(SAMPLE_DOCS))
        assert count == len(SAMPLE_DOCS)

    def test_search_after_index(self):
        r = BM25Retriever()
        asyncio.run(r.index(SAMPLE_DOCS))
        results = asyncio.run(r.search(RetrievalQuery(text="fox", top_k=3)))
        assert len(results) > 0

    def test_search_before_index_empty(self):
        r = BM25Retriever()
        results = asyncio.run(r.search(RetrievalQuery(text="fox", top_k=3)))
        assert results == []

    def test_search_min_score(self):
        r = BM25Retriever()
        asyncio.run(r.index(SAMPLE_DOCS))
        results = asyncio.run(r.search(RetrievalQuery(text="fox", top_k=5, min_score=9999.0)))
        assert results == []

    def test_vault_path_is_available(self):
        """With a vault_index_path set, is_available() returns True even before load."""
        r = BM25Retriever(vault_index_path="/tmp/fake_vault")
        assert r.is_available()

    def test_load_vault_missing_index_noop(self, tmp_path):
        """_load_vault with missing index.json should not crash and leave loaded=False."""
        r = BM25Retriever(vault_index_path=str(tmp_path))
        r._load_vault()
        assert not r._loaded

    def test_load_vault_bad_json(self, tmp_path):
        """_load_vault with malformed index.json should not crash."""
        (tmp_path / "index.json").write_text("not json", encoding="utf-8")
        r = BM25Retriever(vault_index_path=str(tmp_path))
        r._load_vault()
        assert not r._loaded

    def test_load_vault_valid_index(self, tmp_path):
        """_load_vault with valid index + block files should populate the index."""
        blocks_dir = tmp_path / "blocks"
        blocks_dir.mkdir()
        (blocks_dir / "b1.txt").write_text("the quick brown fox", encoding="utf-8")
        (blocks_dir / "b2.txt").write_text("machine learning python", encoding="utf-8")
        index_data = {
            "blocks": {
                "b1": {"source_path": "a.md", "risk_class": "narrative", "raw_tokens": 5},
                "b2": {"source_path": "b.md", "risk_class": "code", "raw_tokens": 3},
            }
        }
        (tmp_path / "index.json").write_text(json.dumps(index_data), encoding="utf-8")
        r = BM25Retriever(vault_index_path=str(tmp_path))
        r._load_vault()
        assert r._loaded
        results = asyncio.run(r.search(RetrievalQuery(text="fox", top_k=3)))
        assert any(res.doc_id == "b1" for res in results)


# ---------------------------------------------------------------------------
# ── fusion.py ────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

from tokenpak.retrieval.fusion import WeightedFusion, rrf_fusion, rrf_fusion_detailed


def _make_rr(doc_id: str, score: float = 1.0) -> RetrievalResult:
    return RetrievalResult(doc_id=doc_id, score=score, content=f"content_{doc_id}")


class TestRRFFusion:
    def test_empty_dict_returns_empty(self):
        assert rrf_fusion({}) == []

    def test_empty_list_returns_empty(self):
        assert rrf_fusion({"bm25": []}) == []

    def test_single_source_order_preserved(self):
        results = [_make_rr(f"d{i}", float(10 - i)) for i in range(5)]
        fused = rrf_fusion({"bm25": results}, top_n=5)
        assert [t[0] for t in fused] == [f"d{i}" for i in range(5)]

    def test_top_n_truncation(self):
        results = [_make_rr(f"d{i}") for i in range(10)]
        fused = rrf_fusion({"bm25": results}, top_n=3)
        assert len(fused) == 3

    def test_deduplication(self):
        a = [_make_rr("shared", 10.0), _make_rr("only_a", 5.0)]
        b = [_make_rr("shared", 0.9), _make_rr("only_b", 0.8)]
        fused = rrf_fusion({"a": a, "b": b}, top_n=10)
        ids = [t[0] for t in fused]
        assert ids.count("shared") == 1

    def test_multi_source_promotes_shared(self):
        bm25_r = [_make_rr("a", 10.0), _make_rr("b", 9.0), _make_rr("c", 8.0)]
        vec_r = [_make_rr("b", 0.9), _make_rr("d", 0.8), _make_rr("a", 0.7)]
        fused = rrf_fusion({"bm25": bm25_r, "vector": vec_r}, top_n=10)
        ids = [t[0] for t in fused]
        # "a" and "b" appear in both lists; "c" and "d" only in one
        assert ids.index("a") < ids.index("c")
        assert ids.index("b") < ids.index("d")

    def test_weighting_affects_ranking(self):
        bm25_r = [_make_rr("bm25_top", 10.0), _make_rr("shared", 5.0)]
        vec_r = [_make_rr("vec_top", 0.95), _make_rr("shared", 0.5)]

        bm25_heavy = rrf_fusion(
            {"bm25": bm25_r, "vector": vec_r},
            weights={"bm25": 10.0, "vector": 0.1}, top_n=5,
        )
        vec_heavy = rrf_fusion(
            {"bm25": bm25_r, "vector": vec_r},
            weights={"bm25": 0.1, "vector": 10.0}, top_n=5,
        )
        assert bm25_heavy[0][0] == "bm25_top"
        assert vec_heavy[0][0] == "vec_top"

    def test_k_lower_amplifies_rank_gap(self):
        results = [_make_rr(f"d{i}", float(10 - i)) for i in range(10)]
        low_k = rrf_fusion({"src": results}, k=1, top_n=10)
        high_k = rrf_fusion({"src": results}, k=1000, top_n=10)
        ratio_low = low_k[0][1] / low_k[-1][1]
        ratio_high = high_k[0][1] / high_k[-1][1]
        assert ratio_low > ratio_high

    def test_returns_best_original_result(self):
        """rrf_fusion should return the result with the highest score as the canonical."""
        high = _make_rr("d1", 10.0)
        low = _make_rr("d1", 1.0)
        fused = rrf_fusion({"a": [low], "b": [high]}, top_n=1)
        assert fused[0][2].score == 10.0

    def test_default_weight_one(self):
        """Missing weights entry defaults to 1.0."""
        results = [_make_rr("d1")]
        fused = rrf_fusion({"bm25": results}, weights=None, top_n=1)
        assert len(fused) == 1


class TestRRFFusionDetailed:
    def test_returns_fused_result_objects(self):
        results = [_make_rr("d1", 5.0)]
        fused = rrf_fusion_detailed({"bm25": results}, top_n=1)
        assert len(fused) == 1
        assert isinstance(fused[0], FusedResult)

    def test_source_results_populated(self):
        r = _make_rr("d1", 5.0)
        fused = rrf_fusion_detailed({"bm25": [r]}, top_n=1)
        assert "bm25" in fused[0].source_results

    def test_empty_returns_empty(self):
        assert rrf_fusion_detailed({}) == []

    def test_multi_source_breakdown(self):
        bm25_r = [_make_rr("d1", 8.0)]
        vec_r = [_make_rr("d1", 0.9), _make_rr("d2", 0.7)]
        fused = rrf_fusion_detailed({"bm25": bm25_r, "vector": vec_r}, top_n=5)
        d1 = next(f for f in fused if f.doc_id == "d1")
        assert "bm25" in d1.source_results
        assert "vector" in d1.source_results

    def test_content_accessible(self):
        r = _make_rr("d1")
        fused = rrf_fusion_detailed({"bm25": [r]}, top_n=1)
        assert fused[0].content == "content_d1"


class TestWeightedFusion:
    def test_fuse_returns_fused_results(self):
        wf = WeightedFusion(k=60, top_n=5)
        results = [_make_rr(f"d{i}") for i in range(3)]
        fused = wf.fuse({"bm25": results})
        assert all(isinstance(f, FusedResult) for f in fused)

    def test_fuse_simple_returns_tuples(self):
        wf = WeightedFusion(k=60, top_n=5)
        results = [_make_rr("d1")]
        simple = wf.fuse_simple({"bm25": results})
        assert len(simple) == 1
        doc_id, score, result = simple[0]
        assert doc_id == "d1"
        assert isinstance(score, float)

    def test_weighted_fusion_matches_direct_call(self):
        results = [_make_rr(f"d{i}", float(10 - i)) for i in range(5)]
        wf = WeightedFusion(weights={"bm25": 1.0}, k=60, top_n=5)
        fused_wf = wf.fuse({"bm25": results})
        direct = rrf_fusion_detailed({"bm25": results}, weights={"bm25": 1.0}, k=60, top_n=5)
        assert [f.doc_id for f in fused_wf] == [f.doc_id for f in direct]

    def test_config_stored(self):
        wf = WeightedFusion(weights={"a": 0.5}, k=30, top_n=10)
        assert wf.k == 30
        assert wf.top_n == 10
        assert wf.weights == {"a": 0.5}


# ---------------------------------------------------------------------------
# ── hybrid.py ────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

from tokenpak.retrieval.hybrid import HybridRetriever


class TestHybridRetrieverInit:
    def test_default_config(self):
        r = HybridRetriever()
        assert r._config is not None
        assert r._bm25 is not None

    def test_custom_config(self):
        cfg = HybridSearchConfig(bm25_weight=0.8, vector_weight=0.2)
        r = HybridRetriever(cfg)
        assert r._config.bm25_weight == 0.8

    def test_not_available_before_index(self):
        r = HybridRetriever()
        if r._vector is not None:
            r._vector._available = False
        assert not r.is_available()

    def test_available_after_index(self):
        r = HybridRetriever()
        if r._vector is not None:
            r._vector._available = False
        asyncio.run(r.index(SAMPLE_DOCS))
        assert r.is_available()


class TestHybridBM25OnlyPath:
    """Tests with vector disabled — exercises pure BM25 path."""

    def _make_bm25_only(self) -> HybridRetriever:
        r = HybridRetriever(HybridSearchConfig())
        if r._vector is not None:
            r._vector._available = False
        return r

    def test_search_returns_fused_results(self):
        r = self._make_bm25_only()
        asyncio.run(r.index(SAMPLE_DOCS))
        results = asyncio.run(r.search("fox", top_k=3))
        assert all(isinstance(fr, FusedResult) for fr in results)

    def test_search_relevant_docs_returned(self):
        r = self._make_bm25_only()
        asyncio.run(r.index(SAMPLE_DOCS))
        results = asyncio.run(r.search("fox", top_k=5))
        ids = {fr.doc_id for fr in results}
        assert "doc1" in ids or "doc4" in ids

    def test_search_empty_before_index(self):
        r = self._make_bm25_only()
        results = asyncio.run(r.search("fox", top_k=3))
        assert results == []

    def test_top_k_respected(self):
        r = self._make_bm25_only()
        asyncio.run(r.index(SAMPLE_DOCS))
        results = asyncio.run(r.search("the fox data", top_k=2))
        assert len(results) <= 2

    def test_no_match_returns_empty(self):
        r = self._make_bm25_only()
        asyncio.run(r.index(SAMPLE_DOCS))
        results = asyncio.run(r.search("xyzzy_nonexistent_token"))
        assert results == []

    def test_fused_scores_positive(self):
        r = self._make_bm25_only()
        asyncio.run(r.index(SAMPLE_DOCS))
        results = asyncio.run(r.search("machine learning", top_k=5))
        assert all(fr.fused_score > 0 for fr in results)


class TestHybridBothSources:
    """Tests with both BM25 and vector mocked."""

    def _make_hybrid_mocked(self):
        r = HybridRetriever(HybridSearchConfig(bm25_weight=0.5, vector_weight=0.5))

        bm25_mock = AsyncMock()
        bm25_mock.search = AsyncMock(return_value=[
            RetrievalResult("doc1", 10.0, "content1", retriever_type=RetrieverType.BM25),
            RetrievalResult("doc2", 5.0, "content2", retriever_type=RetrieverType.BM25),
        ])
        bm25_mock.index = AsyncMock(return_value=2)
        bm25_mock.is_available = MagicMock(return_value=True)
        r._bm25 = bm25_mock

        vec_mock = AsyncMock()
        vec_mock.search = AsyncMock(return_value=[
            RetrievalResult("doc3", 0.95, "content3", retriever_type=RetrieverType.VECTOR),
            RetrievalResult("doc1", 0.8, "content1v", retriever_type=RetrieverType.VECTOR),
        ])
        vec_mock.index = AsyncMock(return_value=2)
        vec_mock._available = True
        vec_mock.is_available = MagicMock(return_value=True)
        r._vector = vec_mock

        return r, bm25_mock, vec_mock

    def test_both_retrievers_called(self):
        r, bm25_mock, vec_mock = self._make_hybrid_mocked()
        asyncio.run(r.search("test", top_k=5))
        bm25_mock.search.assert_called_once()
        vec_mock.search.assert_called_once()

    def test_doc_in_both_lists_ranked_first(self):
        r, _, _ = self._make_hybrid_mocked()
        results = asyncio.run(r.search("test", top_k=5))
        # doc1 appears in both bm25 and vector → should be first
        assert results[0].doc_id == "doc1"

    def test_results_include_all_sources(self):
        r, _, _ = self._make_hybrid_mocked()
        results = asyncio.run(r.search("test", top_k=10))
        ids = {fr.doc_id for fr in results}
        assert "doc1" in ids
        # doc2 (bm25 only) or doc3 (vector only) should also appear
        assert "doc2" in ids or "doc3" in ids

    def test_vector_error_falls_back_to_bm25(self):
        r = HybridRetriever(HybridSearchConfig())
        bm25_mock = AsyncMock()
        bm25_mock.search = AsyncMock(return_value=[
            RetrievalResult("safe_doc", 8.0, "content", retriever_type=RetrieverType.BM25)
        ])
        bm25_mock.is_available = MagicMock(return_value=True)
        r._bm25 = bm25_mock

        vec_mock = AsyncMock()
        vec_mock.search = AsyncMock(side_effect=RuntimeError("vector exploded"))
        vec_mock._available = True
        vec_mock.is_available = MagicMock(return_value=True)
        r._vector = vec_mock

        results = asyncio.run(r.search("query", top_k=3))
        assert len(results) >= 1
        assert results[0].doc_id == "safe_doc"

    def test_bm25_error_in_hybrid_path_falls_back(self):
        """In the hybrid (both sources) path, BM25 errors are caught via asyncio.gather."""
        r = HybridRetriever(HybridSearchConfig())
        bm25_mock = AsyncMock()
        bm25_mock.search = AsyncMock(side_effect=RuntimeError("bm25 failed"))
        bm25_mock.is_available = MagicMock(return_value=True)
        r._bm25 = bm25_mock

        # Vector returns good results
        vec_mock = AsyncMock()
        vec_mock.search = AsyncMock(return_value=[
            RetrievalResult("d1", 0.9, "content", retriever_type=RetrieverType.VECTOR)
        ])
        vec_mock._available = True
        vec_mock.is_available = MagicMock(return_value=True)
        r._vector = vec_mock

        # With vector available, the gather path handles bm25 exception gracefully
        results = asyncio.run(r.search("query", top_k=3))
        assert isinstance(results, list)
        # Vector results should still come through
        assert len(results) >= 1


class TestHybridIndex:
    def test_index_returns_bm25_count(self):
        r = HybridRetriever()
        if r._vector is not None:
            r._vector._available = False
        count = asyncio.run(r.index(SAMPLE_DOCS))
        assert count == len(SAMPLE_DOCS)

    def test_index_with_both_sources(self):
        r = HybridRetriever()
        bm25_mock = AsyncMock()
        bm25_mock.index = AsyncMock(return_value=5)
        r._bm25 = bm25_mock

        vec_mock = AsyncMock()
        vec_mock.index = AsyncMock(return_value=5)
        vec_mock._available = True
        vec_mock.is_available = MagicMock(return_value=True)
        r._vector = vec_mock

        count = asyncio.run(r.index(SAMPLE_DOCS))
        assert count == 5
        bm25_mock.index.assert_called_once()
        vec_mock.index.assert_called_once()


# ---------------------------------------------------------------------------
# ── vault_index.py ────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

from tokenpak.retrieval.vault_index import VaultIndex, _bm25_tokenize


class TestBm25Tokenize:
    def test_basic(self):
        tokens = _bm25_tokenize("Hello World 123")
        assert "hello" in tokens
        assert "world" in tokens
        assert "123" in tokens

    def test_empty(self):
        assert _bm25_tokenize("") == []

    def test_strips_special_chars(self):
        tokens = _bm25_tokenize("foo-bar, baz!")
        assert "foo" in tokens
        assert "bar" in tokens
        assert "," not in tokens

    def test_underscore_preserved(self):
        tokens = _bm25_tokenize("snake_case")
        assert "snake_case" in tokens


def _make_vault_dir(tmp_path: Path, blocks: Dict[str, str]) -> Path:
    """Create a minimal .tokenpak directory with index.json + block files."""
    blocks_dir = tmp_path / "blocks"
    blocks_dir.mkdir()

    index_blocks: Dict[str, Any] = {}
    for bid, content in blocks.items():
        (blocks_dir / f"{bid}.txt").write_text(content, encoding="utf-8")
        index_blocks[bid] = {
            "source_path": f"{bid}.md",
            "risk_class": "narrative",
            "must_keep": False,
            "raw_tokens": max(1, len(content) // 4),
        }

    index_data = {"blocks": index_blocks}
    (tmp_path / "index.json").write_text(json.dumps(index_data), encoding="utf-8")
    return tmp_path


class TestVaultIndexInit:
    def test_not_available_initially(self, tmp_path):
        vi = VaultIndex(str(tmp_path))
        assert not vi.available
        assert not vi.is_ready()

    def test_available_after_load(self, tmp_path):
        _make_vault_dir(tmp_path, {"b1": "hello world foo"})
        vi = VaultIndex(str(tmp_path))
        index_path = tmp_path / "index.json"
        vi._load(index_path, index_path.stat().st_mtime)
        assert vi.available
        assert vi.is_ready()

    def test_doc_count_correct(self, tmp_path):
        _make_vault_dir(tmp_path, {"b1": "foo", "b2": "bar", "b3": "baz"})
        vi = VaultIndex(str(tmp_path))
        index_path = tmp_path / "index.json"
        vi._load(index_path, index_path.stat().st_mtime)
        assert vi._doc_count == 3


class TestVaultIndexSearch:
    def _loaded_vi(self, tmp_path: Path, blocks: Dict[str, str]) -> VaultIndex:
        _make_vault_dir(tmp_path, blocks)
        vi = VaultIndex(str(tmp_path))
        index_path = tmp_path / "index.json"
        vi._load(index_path, index_path.stat().st_mtime)
        return vi

    def test_search_returns_results(self, tmp_path):
        vi = self._loaded_vi(tmp_path, {
            "b1": "the quick brown fox",
            "b2": "machine learning python data science",
        })
        results = vi.search("fox", top_k=5, min_score=0.0)
        assert len(results) > 0

    def test_relevant_block_ranked_first(self, tmp_path):
        vi = self._loaded_vi(tmp_path, {
            "b1": "fox fox fox jumping over",
            "b2": "unrelated content about dogs",
        })
        results = vi.search("fox", top_k=5, min_score=0.0)
        assert results[0][0]["block_id"] == "b1"

    def test_empty_corpus_returns_empty(self, tmp_path):
        vi = VaultIndex(str(tmp_path))
        assert vi.search("anything") == []

    def test_no_match_returns_empty(self, tmp_path):
        vi = self._loaded_vi(tmp_path, {"b1": "hello world foo bar"})
        results = vi.search("xyzzy_nonexistent_token", top_k=5, min_score=0.0)
        assert results == []

    def test_min_score_filters(self, tmp_path):
        vi = self._loaded_vi(tmp_path, {"b1": "fox fox fox", "b2": "other content here"})
        all_r = vi.search("fox", top_k=5, min_score=0.0)
        strict = vi.search("fox", top_k=5, min_score=9999.0)
        assert len(strict) <= len(all_r)

    def test_top_k_respected(self, tmp_path):
        blocks = {f"b{i}": f"token_{i} content text" for i in range(20)}
        vi = self._loaded_vi(tmp_path, blocks)
        results = vi.search("content", top_k=3, min_score=0.0)
        assert len(results) <= 3

    def test_result_structure(self, tmp_path):
        vi = self._loaded_vi(tmp_path, {"b1": "fox runs quickly"})
        results = vi.search("fox", top_k=1, min_score=0.0)
        block, score = results[0]
        assert "block_id" in block
        assert "source_path" in block
        assert isinstance(score, float)

    def test_scores_descending(self, tmp_path):
        vi = self._loaded_vi(tmp_path, {
            "b1": "fox fox fox fox",
            "b2": "fox jumps",
            "b3": "fox once",
        })
        results = vi.search("fox", top_k=5, min_score=0.0)
        scores = [s for _, s in results]
        assert scores == sorted(scores, reverse=True)


class TestVaultIndexCacheStats:
    def test_cache_stats_keys(self, tmp_path):
        _make_vault_dir(tmp_path, {"b1": "hello"})
        vi = VaultIndex(str(tmp_path))
        index_path = tmp_path / "index.json"
        vi._load(index_path, index_path.stat().st_mtime)
        stats = vi.cache_stats
        assert "vault_cache_entries" in stats
        assert "vault_cache_memory_mb" in stats
        assert "vault_cache_hits" in stats
        assert "vault_cache_misses" in stats
        assert "vault_cache_hit_rate" in stats


class TestVaultIndexCompileInjection:
    def test_empty_returns_empty_tuple(self, tmp_path):
        vi = VaultIndex(str(tmp_path))
        text, tokens, refs = vi.compile_injection("query")
        assert text == ""
        assert tokens == 0
        assert refs == []

    def test_returns_content(self, tmp_path):
        _make_vault_dir(tmp_path, {"b1": "fox runs through forest quickly"})
        vi = VaultIndex(str(tmp_path))
        index_path = tmp_path / "index.json"
        vi._load(index_path, index_path.stat().st_mtime)
        text, tokens, refs = vi.compile_injection("fox", budget=500, top_k=3, min_score=0.0)
        assert text != ""
        assert tokens > 0
        assert len(refs) > 0

    def test_budget_respected(self, tmp_path):
        large_content = " ".join(["word"] * 5000)
        _make_vault_dir(tmp_path, {"b1": large_content + " fox"})
        vi = VaultIndex(str(tmp_path))
        index_path = tmp_path / "index.json"
        vi._load(index_path, index_path.stat().st_mtime)
        _, tokens, _ = vi.compile_injection("fox", budget=50, top_k=1, min_score=0.0)
        assert tokens <= 100  # some slack for header tokens


class TestVaultIndexMaybeReload:
    def test_maybe_reload_noop_missing_index(self, tmp_path):
        """maybe_reload with no index.json should not crash."""
        vi = VaultIndex(str(tmp_path))
        vi._last_loaded = 0  # force reload attempt
        vi.maybe_reload()  # should not raise
        assert not vi.available

    def test_maybe_reload_skips_if_recent(self, tmp_path):
        _make_vault_dir(tmp_path, {"b1": "hello"})
        vi = VaultIndex(str(tmp_path))
        import time
        vi._last_loaded = time.time()  # just loaded
        vi.maybe_reload()  # should skip
        # blocks not populated because we skipped reload
        assert len(vi.blocks) == 0


# ---------------------------------------------------------------------------
# ── vector_local.py ──────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

from tokenpak.retrieval.vector_local import LocalVectorRetriever


class TestLocalVectorRetrieverUnavailable:
    """Tests for the graceful-degradation path (no sentence-transformers)."""

    def _make_unavailable(self) -> LocalVectorRetriever:
        r = LocalVectorRetriever()
        r._available = False
        r._loaded = False
        return r

    def test_retriever_type(self):
        assert LocalVectorRetriever().retriever_type == RetrieverType.VECTOR

    def test_is_available_false(self):
        r = self._make_unavailable()
        assert not r.is_available()

    def test_index_returns_zero_when_unavailable(self):
        r = self._make_unavailable()
        count = asyncio.run(r.index(SAMPLE_DOCS))
        assert count == 0

    def test_search_returns_empty_when_unavailable(self):
        r = self._make_unavailable()
        results = asyncio.run(r.search(RetrievalQuery(text="fox", top_k=3)))
        assert results == []

    def test_search_returns_empty_not_loaded(self):
        r = LocalVectorRetriever()
        r._available = True
        r._loaded = False
        r._index_path = None
        # No model loaded and no index path — should return empty without crash
        results = asyncio.run(r.search(RetrievalQuery(text="test", top_k=3)))
        assert isinstance(results, list)


class TestLocalVectorRetrieverMocked:
    """Tests using a mocked SentenceTransformer."""

    def _make_mocked(self) -> LocalVectorRetriever:
        import numpy as np
        r = LocalVectorRetriever(model_name="mock-model")
        r._available = True

        mock_model = MagicMock()
        # encode returns a numpy array
        mock_model.encode = MagicMock(
            return_value=np.array([
                [1.0, 0.0, 0.0],
                [0.9, 0.1, 0.0],
                [0.0, 1.0, 0.0],
                [0.8, 0.2, 0.0],
                [0.0, 0.0, 1.0],
            ], dtype="float32")
        )
        r._model = mock_model
        return r

    @pytest.mark.skipif(
        not __import__("importlib").util.find_spec("numpy"),
        reason="numpy not installed"
    )
    def test_index_populates_state(self):
        r = self._make_mocked()
        count = asyncio.run(r.index(SAMPLE_DOCS))
        assert count == len(SAMPLE_DOCS)
        assert r._loaded
        assert len(r._doc_ids) == len(SAMPLE_DOCS)

    @pytest.mark.skipif(
        not __import__("importlib").util.find_spec("numpy"),
        reason="numpy not installed"
    )
    def test_search_returns_results(self):
        import numpy as np
        r = self._make_mocked()
        asyncio.run(r.index(SAMPLE_DOCS))

        # Mock query encoding
        r._model.encode = MagicMock(
            return_value=np.array([[1.0, 0.0, 0.0]], dtype="float32")
        )
        results = asyncio.run(r.search(RetrievalQuery(text="fox", top_k=3)))
        assert len(results) > 0
        assert all(isinstance(res, RetrievalResult) for res in results)
        assert all(res.retriever_type == RetrieverType.VECTOR for res in results)

    @pytest.mark.skipif(
        not __import__("importlib").util.find_spec("numpy"),
        reason="numpy not installed"
    )
    def test_search_before_index_returns_empty(self):
        r = LocalVectorRetriever()
        r._available = True
        r._loaded = False
        r._index_path = None
        r._model = MagicMock()
        results = asyncio.run(r.search(RetrievalQuery(text="test", top_k=3)))
        assert isinstance(results, list)

    @pytest.mark.skipif(
        not __import__("importlib").util.find_spec("numpy"),
        reason="numpy not installed"
    )
    def test_save_and_load_roundtrip(self, tmp_path):
        import numpy as np
        r = self._make_mocked()
        r._index_path = tmp_path
        asyncio.run(r.index(SAMPLE_DOCS))
        # Check files were written
        assert (tmp_path / "embeddings.npy").exists()
        assert (tmp_path / "doc_ids.txt").exists()

        # Load into a fresh retriever
        r2 = LocalVectorRetriever(index_path=str(tmp_path))
        r2._available = True
        loaded = r2.load()
        assert loaded
        assert r2._loaded
        assert r2._doc_ids == r._doc_ids

    @pytest.mark.skipif(
        not __import__("importlib").util.find_spec("numpy"),
        reason="numpy not installed"
    )
    def test_load_missing_files_returns_false(self, tmp_path):
        r = LocalVectorRetriever(index_path=str(tmp_path))
        r._available = True
        result = r.load()
        assert result is False
        assert not r._loaded
