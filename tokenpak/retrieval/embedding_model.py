"""Pluggable LOCAL embedding-model loader for TIP-cache v2 hybrid retrieval.

This module centralizes embedding-model selection + loading for the
``TOKENPAK_TIP_HYBRID_ENABLED`` path. Two hard constraints (Sue ruling
2026-05-30, standard 49 — privacy):

1. **No network egress.** Loading forces ``HF_HUB_OFFLINE=1`` /
   ``TRANSFORMERS_OFFLINE=1`` before importing sentence-transformers, so a
   missing model fails locally instead of silently downloading from the Hub.
2. **Local-only model choice.** :func:`select_local_model` runs a small
   no-egress benchmark over *already-cached* candidate models and picks a
   CPU-friendly one. It never reaches the network to enumerate or fetch.

The model itself is an optional dependency — every entry point degrades to
``None`` (and the caller falls back to BM25) when sentence-transformers /
numpy are unavailable or no model is cached. Nothing here ever raises.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Default + candidate set. Order is the selection preference for the local
# benchmark: small, CPU-friendly models first. all-MiniLM-L6-v2 (384-dim,
# ~80MB) is the canonical fleet choice and is the only one expected to be
# cached on most hosts.
DEFAULT_MODEL = "all-MiniLM-L6-v2"
CANDIDATE_MODELS: List[str] = [
    "all-MiniLM-L6-v2",
    "paraphrase-MiniLM-L3-v2",
    "all-MiniLM-L12-v2",
]

# Sentences used by the local selection benchmark. Intentionally tiny — this
# measures load + encode latency among cached options, not retrieval quality
# (recall@k is a separate harness, TCV2-05, out of scope for this packet).
_BENCH_SENTENCES = [
    "how does tokenpak handle cache_control markers",
    "vault block storage and BM25 retrieval",
    "hybrid semantic search with reciprocal rank fusion",
]


@dataclass
class EmbeddingModelInfo:
    """Metadata recorded alongside the embeddings artifact (AC: model id / dim / version)."""

    model_id: str
    dim: int
    version: str

    def to_dict(self) -> dict:
        return {"model_id": self.model_id, "dim": self.dim, "version": self.version}


def _enable_offline() -> None:
    """Force sentence-transformers / HF Hub into offline mode (no egress).

    Idempotent; sets the env vars only if not already set so an operator who
    deliberately allows egress elsewhere is not silently overridden mid-process
    (we still never *clear* offline mode — we only ensure it is on by default).
    """
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


def _st_version() -> str:
    try:
        import sentence_transformers  # type: ignore

        return getattr(sentence_transformers, "__version__", "unknown")
    except Exception:
        return "unavailable"


def _hf_cache_dirs() -> List[Path]:
    """Candidate HuggingFace hub cache roots, honoring env overrides."""
    roots: List[Path] = []
    for env_key in ("SENTENCE_TRANSFORMERS_HOME", "HF_HOME", "HUGGINGFACE_HUB_CACHE"):
        val = os.environ.get(env_key)
        if val:
            p = Path(val)
            # HF_HOME points at the parent; the hub cache lives under hub/
            roots.append(p / "hub")
            roots.append(p)
    roots.append(Path.home() / ".cache" / "huggingface" / "hub")
    return roots


def is_model_cached(model_id: str) -> bool:
    """True if ``model_id`` appears in a local HF hub cache (no network)."""
    short = model_id.split("/")[-1]
    needles = {
        f"models--{model_id.replace('/', '--')}",
        f"models--sentence-transformers--{short}",
    }
    for root in _hf_cache_dirs():
        try:
            if not root.is_dir():
                continue
            for entry in root.iterdir():
                if entry.name in needles:
                    return True
        except OSError:
            continue
    # A bare local directory path is also "cached" (operator-supplied model).
    return Path(model_id).is_dir()


def load_model(model_id: Optional[str] = None):
    """Load a SentenceTransformer offline. Returns the model or ``None``.

    Never raises: missing dependency, uncached model, or any load failure all
    return ``None`` so callers fall back to BM25-only retrieval.
    """
    _enable_offline()
    target = model_id or resolve_model_id()
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception as exc:
        logger.debug("embedding_model.load_model: sentence-transformers unavailable: %s", exc)
        return None

    if not is_model_cached(target):
        logger.warning(
            "embedding_model.load_model: model %r not in local cache; "
            "refusing network egress (Std 49). Returning None (BM25 fallback).",
            target,
        )
        return None
    try:
        return SentenceTransformer(target)
    except Exception as exc:
        logger.warning("embedding_model.load_model: failed to load %r offline: %s", target, exc)
        return None


def model_dim(model) -> int:
    """Best-effort embedding dimensionality for a loaded model (0 if unknown)."""
    if model is None:
        return 0
    try:
        dim = model.get_sentence_embedding_dimension()
        if dim:
            return int(dim)
    except Exception:
        pass
    try:
        return int(model.encode(["x"], convert_to_numpy=True).shape[1])
    except Exception:
        return 0


def select_local_model(
    candidates: Optional[List[str]] = None,
    sample_texts: Optional[List[str]] = None,
) -> Optional[EmbeddingModelInfo]:
    """Pick a CPU-friendly local model via a small no-egress benchmark.

    Iterates ``candidates`` (preference order), skips any not locally cached,
    times a small encode over already-cached options, and returns the fastest
    as :class:`EmbeddingModelInfo`. Returns ``None`` if nothing is usable
    locally (caller falls back to BM25). Never reaches the network.
    """
    _enable_offline()
    cands = candidates or CANDIDATE_MODELS
    texts = sample_texts or _BENCH_SENTENCES
    version = _st_version()

    best: Optional[EmbeddingModelInfo] = None
    best_latency = float("inf")
    for model_id in cands:
        if not is_model_cached(model_id):
            logger.debug("select_local_model: %r not cached locally; skipping", model_id)
            continue
        model = load_model(model_id)
        if model is None:
            continue
        try:
            t0 = time.perf_counter()
            vecs = model.encode(texts, show_progress_bar=False, convert_to_numpy=True)
            latency = time.perf_counter() - t0
            dim = int(vecs.shape[1])
        except Exception as exc:
            logger.debug("select_local_model: encode failed for %r: %s", model_id, exc)
            continue
        logger.info(
            "select_local_model: candidate %r ok (dim=%d, %.3fs for %d sentences)",
            model_id, dim, latency, len(texts),
        )
        if latency < best_latency:
            best_latency = latency
            best = EmbeddingModelInfo(model_id=model_id, dim=dim, version=version)

    if best is None:
        logger.warning(
            "select_local_model: no locally-cached embedding model among %s; "
            "hybrid retrieval will fall back to BM25.", cands,
        )
    else:
        logger.info("select_local_model: selected %r (dim=%d)", best.model_id, best.dim)
    return best


def resolve_model_id() -> str:
    """Resolve the embedding model id to load.

    Precedence: ``TOKENPAK_EMBEDDING_MODEL`` env override → first locally-cached
    candidate from the benchmark → :data:`DEFAULT_MODEL`. The default is
    returned even when uncached so :func:`load_model`'s offline guard produces
    the single explanatory warning rather than this resolver guessing silently.
    """
    override = os.environ.get("TOKENPAK_EMBEDDING_MODEL")
    if override:
        return override
    selected = select_local_model()
    if selected is not None:
        return selected.model_id
    return DEFAULT_MODEL
