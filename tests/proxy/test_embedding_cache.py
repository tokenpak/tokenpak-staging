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


def test_size_eviction_deletes_the_oldest_row_first(monkeypatch):
    deleted: list[str] = []

    class FakeRow:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return (self.value,)

    class FakeConnection:
        def __init__(self):
            self.page_checks = 0

        def execute(self, sql, params=()):
            if "pragma_page_count" in sql:
                self.page_checks += 1
                return FakeRow(2 * 1024 * 1024 if self.page_checks == 1 else 0)
            if "ORDER BY created_at ASC LIMIT 1" in sql:
                deleted.append("oldest")
                return FakeRow(None)
            if "SELECT COUNT(*)" in sql:
                return FakeRow(1)
            raise AssertionError(f"unexpected SQL: {sql}")

        def commit(self):
            return None

        def close(self):
            return None

    fake = FakeConnection()
    monkeypatch.setattr(ec, "_connect", lambda _db_path: fake)
    cache = object.__new__(ec.EmbeddingCache)
    cache.db_path = ":memory:"
    cache.max_mb = 1

    cache._evict()

    assert deleted == ["oldest"]
