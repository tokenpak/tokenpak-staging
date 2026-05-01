"""TokenPak Agent Vault Block Storage — JSON-format block persistence.

TCM-04 (2026-04-29): adds ``BlockStore.default()`` classmethod that
returns a vault-backed store reading the canonical index at
``~/vault/.tokenpak/`` (override via ``TOKENPAK_VAULT_INDEX_PATH``).
Also adds an env-var-gated BM25 search path (``TOKENPAK_TIP_BM25_ENABLED``)
that delegates to :class:`tokenpak.retrieval.vault_index.VaultIndex`.
The legacy substring-counter ``search()`` remains as the in-memory
fallback for unit tests with explicit fixtures.

See standard 26-tip-cache.md §2 for the canonicality contract.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class BlockRecord:
    """A compressed content block stored on disk."""

    block_id: str  # Typically path#hash[:8]
    path: str  # Source file path
    content_hash: str  # SHA256 of original content
    file_type: str  # "code" | "text" | "data"
    raw_tokens: int
    compressed_tokens: int
    compressed_content: str
    quality_score: float = 1.0
    indexed_at: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    @property
    def compression_ratio(self) -> float:
        if self.raw_tokens == 0:
            return 1.0
        return self.compressed_tokens / self.raw_tokens

    @property
    def tokens_saved(self) -> int:
        return max(0, self.raw_tokens - self.compressed_tokens)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["compression_ratio"] = self.compression_ratio
        d["tokens_saved"] = self.tokens_saved
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "BlockRecord":
        # Drop derived fields that aren't constructor params
        data = {k: v for k, v in data.items() if k not in ("compression_ratio", "tokens_saved")}
        return cls(**data)


class BlockStore:
    """JSON-backed block storage for compressed file content.

    Each collection is stored as a single JSON file (suitable for small-medium
    vaults). For large vaults, Phase 1 introduces SQLite persistence.

    Usage::

        store = BlockStore("~/.tokenpak/blocks.json")
        store.save(record)
        block = store.get("path/to/file.py#abc123")
        results = store.search("token compression", top_k=5)
        store.flush()
    """

    def __init__(self, store_path: str = ":memory:"):
        self._path = store_path
        self._blocks: dict[str, BlockRecord] = {}
        # TCM-04: optional BM25-backed reader for vault retrieval. Set by
        # default() when the canonical vault index is loaded; None for
        # in-memory test stores.
        self._vault_index = None  # type: Optional[Any]

        if store_path != ":memory:":
            self._load()

    # ------------------------------------------------------------------
    # Canonical entry point (TCM-04, see standard 26-tip-cache.md §2)
    # ------------------------------------------------------------------

    _default_lock = threading.Lock()
    _default_instance: Optional["BlockStore"] = None

    @classmethod
    def default(cls) -> "BlockStore":
        """Return a vault-backed BlockStore reading ``~/vault/.tokenpak/``.

        Behavior contract (per standard 26 §2):

        - Reads ``TOKENPAK_VAULT_INDEX_PATH`` env (default ``~/vault/.tokenpak``).
        - Cached at module level — first call loads, subsequent calls
          return the cached instance.
        - Returns an empty store (with single warning log) if
          ``TOKENPAK_TIP_BM25_ENABLED != "1"``. Default is OFF.
        - Returns an empty store (with warning) if vault index files
          are missing or corrupted. **Never raises.**

        Callers: ``companion/hooks/pre_send.py`` and
        ``services/routing_service/context_enrichment.py``.
        """
        # Fast path
        if cls._default_instance is not None:
            return cls._default_instance

        with cls._default_lock:
            # Double-check inside lock
            if cls._default_instance is not None:
                return cls._default_instance

            store = cls(":memory:")  # in-memory legacy store as base

            enabled = os.environ.get("TOKENPAK_TIP_BM25_ENABLED", "").lower() in ("1", "true", "yes")
            if not enabled:
                logger.info(
                    "BlockStore.default(): TOKENPAK_TIP_BM25_ENABLED is off; "
                    "returning empty store (no vault retrieval)"
                )
                cls._default_instance = store
                return store

            index_path = os.environ.get(
                "TOKENPAK_VAULT_INDEX_PATH",
                str(Path.home() / "vault" / ".tokenpak"),
            )
            if not Path(index_path).exists():
                logger.warning(
                    "BlockStore.default(): vault index path %s does not exist; "
                    "returning empty store",
                    index_path,
                )
                cls._default_instance = store
                return store

            try:
                from tokenpak.retrieval.vault_index import VaultIndex

                vi = VaultIndex(index_path)
                vi.maybe_reload()
                # Wait briefly for the background load to settle.
                # VaultIndex loads inline if no cache; ~30s tops.
                deadline = time.monotonic() + 30.0
                while not vi.is_ready and time.monotonic() < deadline:
                    time.sleep(0.1)
                if not vi.is_ready:
                    logger.warning(
                        "BlockStore.default(): vault index not ready after 30s; "
                        "returning empty store"
                    )
                    cls._default_instance = store
                    return store
                store._vault_index = vi
                logger.info(
                    "BlockStore.default(): vault index loaded from %s", index_path
                )
            except Exception as exc:
                logger.warning(
                    "BlockStore.default(): failed to load vault index from %s: %s; "
                    "returning empty store",
                    index_path,
                    exc,
                )

            cls._default_instance = store
            return store

    @classmethod
    def reset_default_for_tests(cls) -> None:
        """Test-only: drop the cached default instance so tests can
        override TOKENPAK_TIP_BM25_ENABLED / TOKENPAK_VAULT_INDEX_PATH
        and re-trigger a fresh load on the next ``default()`` call."""
        with cls._default_lock:
            cls._default_instance = None

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def save(self, record: BlockRecord) -> None:
        """Upsert a block record."""
        self._blocks[record.block_id] = record
        if self._path != ":memory:":
            self.flush()

    def get(self, block_id: str) -> Optional[BlockRecord]:
        return self._blocks.get(block_id)

    def get_by_path(self, path: str) -> list[BlockRecord]:
        return [b for b in self._blocks.values() if b.path == path]

    def delete(self, block_id: str) -> bool:
        if block_id in self._blocks:
            del self._blocks[block_id]
            if self._path != ":memory:":
                self.flush()
            return True
        return False

    def all(self) -> list[BlockRecord]:
        return list(self._blocks.values())

    def search(self, query: str, top_k: int = 10) -> list[BlockRecord]:
        """BM25 search via vault_index when available; substring fallback otherwise.

        - If this store was built via :meth:`default` AND the vault
          index loaded successfully, delegate to
          :class:`tokenpak.retrieval.vault_index.VaultIndex.search`.
          Returns BlockRecord-shaped results constructed from the
          vault index hits.
        - Otherwise, fall back to substring counting over in-memory
          ``_blocks`` (legacy behavior; intended for unit tests with
          explicit fixtures).
        """
        if self._vault_index is not None:
            try:
                # VaultIndex.search returns list of (block_dict, score) tuples
                hits = self._vault_index.search(query, top_k=top_k)
                results: list[BlockRecord] = []
                for hit in hits:
                    if isinstance(hit, tuple) and len(hit) == 2:
                        block_dict, score = hit
                    elif isinstance(hit, dict):
                        block_dict, score = hit, 0.0
                    else:
                        continue
                    # Read content from the corresponding blocks/<id>.txt
                    block_id = block_dict.get("block_id", "")
                    content = self._read_block_content(block_id)
                    rec = BlockRecord(
                        block_id=block_id,
                        path=block_dict.get("source_path", ""),
                        content_hash=block_dict.get("content_hash", ""),
                        file_type=block_dict.get("source_type", "text"),
                        raw_tokens=int(block_dict.get("raw_tokens", 0)),
                        compressed_tokens=int(block_dict.get("raw_tokens", 0)),
                        compressed_content=content,
                        quality_score=float(score),
                        metadata={"score": float(score)},
                    )
                    # Backward-compat alias used by some callers
                    setattr(rec, "text", content)
                    results.append(rec)
                return results
            except Exception as exc:
                logger.warning(
                    "BlockStore.search(): vault_index delegation failed: %s; "
                    "falling back to substring search",
                    exc,
                )

        # Substring fallback
        q = query.lower()
        scored = []
        for block in self._blocks.values():
            score = block.compressed_content.lower().count(q)
            if score > 0:
                scored.append((score, block))
        scored.sort(key=lambda x: -x[0])
        return [b for _, b in scored[:top_k]]

    def _read_block_content(self, block_id: str) -> str:
        """Read the content file for a vault block.

        Vault index stores metadata in ``index.json`` and content in
        ``blocks/<block_id>.txt``. Returns empty string on any error.
        """
        if self._vault_index is None or not block_id:
            return ""
        try:
            blocks_dir = Path(self._vault_index.tokenpak_dir) / "blocks"
            content_file = blocks_dir / f"{block_id}.txt"
            if not content_file.exists():
                return ""
            return content_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

    def stats(self) -> dict[str, Any]:
        blocks = list(self._blocks.values())
        total_raw = sum(b.raw_tokens for b in blocks)
        total_compressed = sum(b.compressed_tokens for b in blocks)
        return {
            "total_blocks": len(blocks),
            "total_raw_tokens": total_raw,
            "total_compressed_tokens": total_compressed,
            "total_tokens_saved": total_raw - total_compressed,
            "avg_compression_ratio": (
                sum(b.compression_ratio for b in blocks) / len(blocks) if blocks else 1.0
            ),
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def flush(self) -> None:
        """Write blocks to the JSON store file."""
        path = Path(self._path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {bid: block.to_dict() for bid, block in self._blocks.items()}
        path.write_text(json.dumps(data, indent=2))

    def _load(self) -> None:
        path = Path(self._path).expanduser()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            self._blocks = {bid: BlockRecord.from_dict(bd) for bid, bd in data.items()}
        except (json.JSONDecodeError, KeyError, TypeError):
            self._blocks = {}

    def __len__(self) -> int:
        return len(self._blocks)


_store: Optional[BlockStore] = None


def get_block_store(store_path: str = ":memory:") -> BlockStore:
    """Return the process-level singleton block store."""
    global _store
    if _store is None:
        _store = BlockStore(store_path)
    return _store


# ---------------------------------------------------------------------------
# Slice storage (mirrors BlockStore but for SliceRecord objects)
# ---------------------------------------------------------------------------


class SliceStore:
    """In-memory + optional JSON persistence for :class:`~tokenpak.agent.vault.slicer.SliceRecord`.

    Keeps an index keyed by ``slice_id`` and a secondary index from
    ``parent_block_id`` → list of slice IDs for efficient provenance lookup.

    Usage::

        store = SliceStore(":memory:")
        store.save(slice_record)
        children = store.get_by_parent("path/to/doc.md#abc123")
        results = store.search("Script 1")
    """

    def __init__(self, store_path: str = ":memory:"):
        # Import here to avoid circular-import at module load time
        from tokenpak.agent.vault.slicer import SliceRecord as _SR  # noqa: F401

        self._path = store_path
        self._slices: dict[str, Any] = {}  # slice_id → SliceRecord
        self._parent_index: dict[str, list[str]] = {}  # parent_block_id → [slice_ids]

        if store_path != ":memory:":
            self._load()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def save(self, record: Any) -> None:
        """Upsert a slice record."""
        old = self._slices.get(record.slice_id)
        if old is not None:
            # Remove from old parent index if parent changed (shouldn't happen normally)
            old_parent = old.parent_block_id
            if (
                old_parent in self._parent_index
                and record.slice_id in self._parent_index[old_parent]
            ):
                self._parent_index[old_parent].remove(record.slice_id)

        self._slices[record.slice_id] = record
        self._parent_index.setdefault(record.parent_block_id, [])
        if record.slice_id not in self._parent_index[record.parent_block_id]:
            self._parent_index[record.parent_block_id].append(record.slice_id)

        if self._path != ":memory:":
            self.flush()

    def get(self, slice_id: str) -> Optional[Any]:
        return self._slices.get(slice_id)

    def get_by_parent(self, parent_block_id: str) -> list:
        """Return all slices for a given parent block ID, ordered by slice_index."""
        ids = self._parent_index.get(parent_block_id, [])
        records = [self._slices[sid] for sid in ids if sid in self._slices]
        return sorted(records, key=lambda r: r.slice_index)

    def get_by_path(self, path: str) -> list:
        """Return all slices whose parent_path matches."""
        return [r for r in self._slices.values() if r.parent_path == path]

    def delete_by_parent(self, parent_block_id: str) -> int:
        """Remove all slices for a parent block. Returns count removed."""
        ids = list(self._parent_index.pop(parent_block_id, []))
        for sid in ids:
            self._slices.pop(sid, None)
        if ids and self._path != ":memory:":
            self.flush()
        return len(ids)

    def all(self) -> list:
        return list(self._slices.values())

    def search(self, query: str, top_k: int = 10) -> list:
        """Keyword search over slice content (multi-term, case-insensitive TF scoring)."""
        import re as _re

        terms = [t for t in _re.split(r"\W+", query.lower()) if len(t) > 2]
        if not terms:
            return []
        scored = []
        for record in self._slices.values():
            text = record.content.lower()
            score = sum(text.count(t) for t in terms)
            if score > 0:
                scored.append((score, record))
        scored.sort(key=lambda x: -x[0])
        return [r for _, r in scored[:top_k]]

    def stats(self) -> dict[str, Any]:
        slices = list(self._slices.values())
        return {
            "total_slices": len(slices),
            "unique_parents": len(self._parent_index),
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def flush(self) -> None:
        """Write slices to the JSON store file."""
        import json
        from dataclasses import asdict

        path = Path(self._path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {sid: asdict(r) for sid, r in self._slices.items()}
        path.write_text(json.dumps(data, indent=2))

    def _load(self) -> None:
        import json

        from tokenpak.agent.vault.slicer import SliceRecord

        path = Path(self._path).expanduser()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            for sid, d in data.items():
                r = SliceRecord(**d)
                self._slices[sid] = r
                self._parent_index.setdefault(r.parent_block_id, [])
                if sid not in self._parent_index[r.parent_block_id]:
                    self._parent_index[r.parent_block_id].append(sid)
        except (json.JSONDecodeError, KeyError, TypeError):
            self._slices = {}
            self._parent_index = {}

    def __len__(self) -> int:
        return len(self._slices)
