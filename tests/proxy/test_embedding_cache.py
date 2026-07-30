# SPDX-License-Identifier: Apache-2.0
"""Regression tests for embedding-cache keying, TTL, and eviction ordering."""

from __future__ import annotations

import sqlite3
import time

from tokenpak.proxy import embedding_cache as ec


def test_cache_key_includes_model_dimensions_and_text():
    keys = {
        ec._cache_key("text-embedding-3-small", 1536, "hello"),
        ec._cache_key("text-embedding-3-large", 1536, "hello"),
        ec._cache_key("text-embedding-3-small", 3072, "hello"),
        ec._cache_key("text-embedding-3-small", 1536, "hello!"),
    }

    assert len(keys) == 4
    assert all(len(key) == 64 for key in keys)


def test_identical_embedding_triple_round_trips(tmp_path):
    cache = ec.EmbeddingCache(str(tmp_path / "embeddings.sqlite"), ttl_days=7)

    cache.put("text-embedding-3-small", 1536, "hello", b"[1,2,3]", tokens=3)

    assert cache.get("text-embedding-3-small", 1536, "hello") == b"[1,2,3]"
    assert cache.get("text-embedding-3-small", 1536, "hello", no_cache=True) is None


def test_expired_embedding_returns_none(tmp_path):
    db_path = tmp_path / "embeddings.sqlite"
    cache = ec.EmbeddingCache(str(db_path), ttl_days=1)
    cache.put("text-embedding-3-small", 1536, "hello", b"[1,2,3]", tokens=3)

    old = int(time.time()) - 2 * 86400
    with sqlite3.connect(db_path) as con:
        con.execute("UPDATE cache SET created_at = ?", (old,))
        con.commit()

    assert cache.get("text-embedding-3-small", 1536, "hello") is None


def test_size_eviction_deletes_the_oldest_row_first(tmp_path):
    cache = ec.EmbeddingCache(str(tmp_path / "embeddings.sqlite"), max_mb=1)
    first = b"a" * 600_000
    second = b"b" * 600_000

    cache.put("text-embedding-3-small", 1536, "first", first, tokens=1)
    cache.put("text-embedding-3-small", 1536, "second", second, tokens=1)

    assert cache.get("text-embedding-3-small", 1536, "first") is None
    assert cache.get("text-embedding-3-small", 1536, "second") == second
