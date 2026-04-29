"""Embedding indexer — V4 prototype.

Wraps :class:`tokenpak.retrieval.vector_local.LocalVectorRetriever` with a
synchronous builder + searcher that reads from the canonical vault index
layout at ``~/vault/.tokenpak/`` (TCM-02 / standard 26 §1).

This module is the **V4 prototype** introduced by initiative
``2026-04-29-tip-cache-v2-semantic`` (TCV2). It demonstrates that the
embedding layer can be built against the real vault corpus and answer
queries; full hybrid wiring + activation gating is TCV2-04 work.

Usage::

    from tokenpak.retrieval.embedding_indexer import EmbeddingIndexer
    idx = EmbeddingIndexer.from_vault_dir("/home/sue/vault/.tokenpak")
    idx.build_if_stale()
    hits = idx.search("how does tokenpak handle cache_control markers", top_k=5)
    for hit in hits:
        print(hit["block_id"], hit["score"], hit["source_path"])

Storage layout (under ``<vault_dir>/embeddings/``, *separate* from the
BM25 cache to avoid format coupling)::

    embeddings.npy        — float32 (N, dim)
    doc_ids.txt           — one block_id per line
    contents.txt          — one content per line (newline-escaped)
    meta.json             — per-block metadata (source_path, content_hash)
    embeddings.meta.json  — model name, dim, generation timestamp, source index_hash

Falls back to empty results if ``sentence-transformers`` or ``numpy`` is
unavailable; never raises.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class EmbeddingHit:
    """Result row from EmbeddingIndexer.search()."""

    block_id: str
    score: float
    source_path: str
    content: str
    content_hash: str


class EmbeddingIndexer:
    """Build + query semantic embeddings over a vault index directory.

    Reads block metadata from ``<vault_dir>/index.json`` and content from
    ``<vault_dir>/blocks/<block_id>.txt``. Writes embeddings to
    ``<vault_dir>/embeddings/`` so the BM25 cache (.bm25_cache.pkl) is
    untouched.
    """

    DEFAULT_MODEL = "all-MiniLM-L6-v2"

    def __init__(
        self,
        vault_dir: Path,
        model_name: str = DEFAULT_MODEL,
    ) -> None:
        self.vault_dir = Path(vault_dir)
        self.embeddings_dir = self.vault_dir / "embeddings"
        self.model_name = model_name
        self._retriever: Any = None
        self._meta_path = self.embeddings_dir / "embeddings.meta.json"

    @classmethod
    def from_vault_dir(cls, vault_dir: str | Path) -> "EmbeddingIndexer":
        return cls(Path(vault_dir))

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build_if_stale(self, *, force: bool = False) -> Dict[str, Any]:
        """Rebuild the embedding index if the source ``index.json`` has
        changed since the last build (or if ``force`` is True).

        Returns a stats dict: ``{"built": bool, "blocks": int, "dim": int,
        "duration_s": float, "reason": str}``.
        """
        index_json = self.vault_dir / "index.json"
        if not index_json.exists():
            return {"built": False, "blocks": 0, "dim": 0, "duration_s": 0.0,
                    "reason": "no index.json"}

        # Hash the source index.json so we know when to rebuild
        source_hash = hashlib.md5(index_json.read_bytes()).hexdigest()

        if not force and self._meta_path.exists():
            try:
                prior = json.loads(self._meta_path.read_text())
                if prior.get("index_hash") == source_hash:
                    return {"built": False, "blocks": prior.get("blocks", 0),
                            "dim": prior.get("dim", 0), "duration_s": 0.0,
                            "reason": "fresh"}
            except Exception:
                pass  # fall through and rebuild

        # Initialize the retriever lazily
        retriever = self._get_retriever()
        if retriever is None:
            return {"built": False, "blocks": 0, "dim": 0, "duration_s": 0.0,
                    "reason": "vector backend unavailable"}

        # Load blocks
        documents = self._load_documents(index_json)
        if not documents:
            return {"built": False, "blocks": 0, "dim": 0, "duration_s": 0.0,
                    "reason": "no documents"}

        t0 = time.monotonic()
        try:
            n_indexed = asyncio.run(retriever.index(documents))
        except Exception as exc:
            logger.warning("EmbeddingIndexer.build failed: %s", exc)
            return {"built": False, "blocks": 0, "dim": 0, "duration_s": 0.0,
                    "reason": f"index error: {exc}"}
        duration = time.monotonic() - t0

        embeddings = retriever._embeddings  # internal but stable for prototype
        dim = int(embeddings.shape[1]) if embeddings is not None and hasattr(embeddings, "shape") else 0

        # Persist meta sidecar
        self.embeddings_dir.mkdir(parents=True, exist_ok=True)
        self._meta_path.write_text(json.dumps({
            "model": self.model_name,
            "dim": dim,
            "blocks": n_indexed,
            "index_hash": source_hash,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        }, indent=2))

        return {"built": True, "blocks": n_indexed, "dim": dim,
                "duration_s": round(duration, 1), "reason": "rebuilt"}

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str, top_k: int = 5) -> List[EmbeddingHit]:
        """Return top-K semantic hits as :class:`EmbeddingHit`."""
        retriever = self._get_retriever()
        if retriever is None:
            return []
        try:
            from .base import RetrievalQuery
            results = asyncio.run(retriever.search(RetrievalQuery(text=query, top_k=top_k)))
        except Exception as exc:
            logger.warning("EmbeddingIndexer.search failed: %s", exc)
            return []

        hits: List[EmbeddingHit] = []
        for r in results:
            meta = getattr(r, "metadata", {}) or {}
            hits.append(EmbeddingHit(
                block_id=getattr(r, "doc_id", "") or meta.get("block_id", ""),
                score=float(getattr(r, "score", 0.0) or 0.0),
                source_path=meta.get("source_path", ""),
                content=getattr(r, "content", "") or "",
                content_hash=meta.get("content_hash", ""),
            ))
        return hits

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_retriever(self) -> Any:
        if self._retriever is not None:
            return self._retriever
        try:
            from .vector_local import LocalVectorRetriever
        except ImportError:
            return None
        retriever = LocalVectorRetriever(
            model_name=self.model_name,
            index_path=str(self.embeddings_dir),
        )
        if not retriever.is_available():
            return None
        self._retriever = retriever
        return retriever

    def _load_documents(self, index_json: Path) -> List[Dict[str, Any]]:
        """Read index.json + blocks/*.txt → list of {id, content, ...}."""
        try:
            data = json.loads(index_json.read_text())
        except Exception as exc:
            logger.warning("Could not parse %s: %s", index_json, exc)
            return []
        blocks_root = self.vault_dir / "blocks"
        documents: List[Dict[str, Any]] = []
        for block_id, meta in (data.get("blocks") or {}).items():
            content_file = blocks_root / f"{block_id}.txt"
            if not content_file.exists():
                continue
            try:
                content = content_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if not content.strip():
                continue
            documents.append({
                "id": block_id,
                "content": content,
                "source_path": meta.get("source_path", ""),
                "content_hash": meta.get("content_hash", ""),
            })
        return documents


__all__ = ["EmbeddingIndexer", "EmbeddingHit"]
