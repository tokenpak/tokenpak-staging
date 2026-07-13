"""Regression coverage for post-publication vault reload memory return."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import textwrap
import time
import weakref
from pathlib import Path

import pytest

from tokenpak.proxy import vault_bridge


def _write_generation(root: Path, generation: int) -> tuple[Path, dict[str, str]]:
    blocks_dir = root / "blocks"
    blocks_dir.mkdir(exist_ok=True)
    contents = {
        f"g{generation}-primary": f"needle needle primary marker{generation}",
        f"g{generation}-secondary": f"needle secondary secondary marker{generation}",
    }
    metadata: dict[str, dict[str, object]] = {}
    for block_id, content in contents.items():
        content_path = blocks_dir / f"{block_id}.txt"
        content_path.write_text(content, encoding="utf-8")
        metadata[block_id] = {
            "source_path": f"docs/{block_id}.md",
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            "raw_tokens": len(content.split()),
        }

    index_path = root / "index.json"
    index_path.write_text(json.dumps({"blocks": metadata}), encoding="utf-8")
    stamp = time.time() + generation + 1
    os.utime(index_path, (stamp, stamp))
    return index_path, contents


def test_reload_returns_memory_only_after_successful_publication(tmp_path, monkeypatch):
    """Regression: collection follows publication, not failed or unchanged reload attempts."""
    monkeypatch.setattr(vault_bridge, "VAULT_INDEX_RELOAD_INTERVAL", 0)
    monkeypatch.setattr(vault_bridge, "_VAULT_CACHE_PRELOAD", 0)
    index_path, _ = _write_generation(tmp_path, 1)
    index = vault_bridge.VaultIndex(str(tmp_path))
    old_generation_ref = weakref.ref(index._snapshot_generation())
    release_observations: list[bool] = []

    def record_release() -> None:
        release_observations.append(old_generation_ref() is None)

    monkeypatch.setattr(vault_bridge, "_return_released_memory_to_os", record_release)
    index.maybe_reload()

    assert index._snapshot_generation().generation_id == 1
    assert release_observations == [True]

    index.maybe_reload()
    assert release_observations == [True]

    index_path.write_text("{not-json", encoding="utf-8")
    failed_stamp = time.time() + 20
    os.utime(index_path, (failed_stamp, failed_stamp))
    index.maybe_reload()

    assert index._snapshot_generation().generation_id == 1
    assert release_observations == [True]


def test_memory_return_uses_glibc_trim_after_collection(monkeypatch):
    """Linux/glibc returns allocator pages only after Python collection."""
    events: list[object] = []

    class FakeTrim:
        argtypes: object = None
        restype: object = None

        def __call__(self, pad: int) -> int:
            events.append(("trim", pad))
            return 1

    class FakeLibc:
        malloc_trim = FakeTrim()

    monkeypatch.setattr(vault_bridge.gc, "collect", lambda: events.append("collect"))
    monkeypatch.setattr(vault_bridge.sys, "platform", "linux")
    monkeypatch.setattr(vault_bridge.platform, "libc_ver", lambda: ("glibc", "2.39"))
    monkeypatch.setattr(vault_bridge.ctypes, "CDLL", lambda _name: FakeLibc())

    vault_bridge._return_released_memory_to_os()

    assert events == ["collect", ("trim", 0)]
    assert FakeLibc.malloc_trim.argtypes == [vault_bridge.ctypes.c_size_t]
    assert FakeLibc.malloc_trim.restype is vault_bridge.ctypes.c_int


@pytest.mark.parametrize(
    ("system_platform", "libc_name"),
    [("darwin", "glibc"), ("linux", "musl")],
)
def test_memory_return_is_no_fail_without_linux_glibc(
    monkeypatch, system_platform: str, libc_name: str
):
    """Unsupported allocators still collect Python objects without loading libc."""
    events: list[str] = []

    def unexpected_cdll(_name):
        raise AssertionError("malloc_trim must stay platform-gated")

    monkeypatch.setattr(vault_bridge.gc, "collect", lambda: events.append("collect"))
    monkeypatch.setattr(vault_bridge.sys, "platform", system_platform)
    monkeypatch.setattr(vault_bridge.platform, "libc_ver", lambda: (libc_name, "1.0"))
    monkeypatch.setattr(vault_bridge.ctypes, "CDLL", unexpected_cdll)

    vault_bridge._return_released_memory_to_os()

    assert events == ["collect"]


def test_memory_return_is_no_fail_when_glibc_trim_is_unavailable(monkeypatch):
    """A Linux runtime without an exported malloc_trim remains a supported no-op."""
    monkeypatch.setattr(vault_bridge.gc, "collect", lambda: 0)
    monkeypatch.setattr(vault_bridge.sys, "platform", "linux")
    monkeypatch.setattr(vault_bridge.platform, "libc_ver", lambda: ("glibc", "2.39"))

    def unavailable_cdll(_name):
        raise OSError("symbol unavailable")

    monkeypatch.setattr(vault_bridge.ctypes, "CDLL", unavailable_cdll)

    vault_bridge._return_released_memory_to_os()


def test_six_generations_preserve_search_cache_and_injection_bytes(tmp_path, monkeypatch):
    """Six publications invalidate old cache entries without changing retrieval behavior."""
    monkeypatch.setattr(vault_bridge, "VAULT_INDEX_RELOAD_INTERVAL", 0)
    monkeypatch.setattr(vault_bridge, "_VAULT_CACHE_MAX_BYTES", 512)
    monkeypatch.setattr(vault_bridge, "_VAULT_CACHE_PRELOAD", 0)
    index = vault_bridge.VaultIndex(str(tmp_path))
    idf = math.log(1.2)
    expected_scores = [idf * 5 / 3.5, idf]

    for generation in range(1, 7):
        previous_cache = index._content_cache
        _, contents = _write_generation(tmp_path, generation)
        index.maybe_reload()
        snapshot = index._snapshot_generation()
        expected_ids = (f"g{generation}-primary", f"g{generation}-secondary")

        assert snapshot.generation_id == generation
        assert snapshot.block_ids == expected_ids
        assert index._content_cache is not previous_cache
        assert tuple(index._content_cache) == ()

        results = index.search("needle", top_k=2, min_score=0)
        assert [block["block_id"] for block, _score in results] == list(expected_ids)
        assert [score for _block, score in results] == pytest.approx(expected_scores, rel=1e-12)
        assert [block["content"] for block, _score in results] == [
            contents[block_id] for block_id in expected_ids
        ]

        injection, tokens_used, refs = index.compile_injection(
            "needle", budget=1000, top_k=2, min_score=0
        )
        expected_injection = (
            "\n\n## Retrieved Context\n"
            f"--- [docs/{expected_ids[0]}.md] (relevance: 0.3) ---\n"
            f"{contents[expected_ids[0]]}\n\n"
            f"--- [docs/{expected_ids[1]}.md] (relevance: 0.2) ---\n"
            f"{contents[expected_ids[1]]}"
        )
        expected_cache_bytes = sum(len(content.encode()) for content in contents.values())

        assert injection.encode() == expected_injection.encode()
        assert tokens_used == vault_bridge.count_tokens(expected_injection)
        assert refs == [f"docs/{block_id}.md" for block_id in expected_ids]
        assert index._cache_bytes == expected_cache_bytes
        assert index._cache_bytes <= index._max_cache_bytes == 512
        assert all(key[0] == generation for key in index._content_cache)


@pytest.mark.skipif(
    sys.platform != "linux" or platform.libc_ver()[0].lower() != "glibc",
    reason="RSS return assertion requires Linux /proc and glibc malloc_trim",
)
def test_linux_glibc_reload_rss_has_no_positive_ratchet():
    """Six measured reloads stay within an 8 MiB allocator noise envelope."""
    script = textwrap.dedent(
        """
        import json
        import os
        import tempfile
        import time
        from pathlib import Path

        from tokenpak.proxy import vault_bridge

        def rss_kib():
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
            raise RuntimeError("VmRSS missing from /proc/self/status")

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            blocks_dir = root / "blocks"
            blocks_dir.mkdir()
            metadata = {}
            for block_number in range(96):
                block_id = f"block-{block_number}"
                content = " ".join(
                    f"term_{block_number}_{term_number}" for term_number in range(700)
                )
                (blocks_dir / f"{block_id}.txt").write_text(content, encoding="utf-8")
                metadata[block_id] = {
                    "source_path": f"docs/{block_id}.md",
                    "raw_tokens": 700,
                }

            index_path = root / "index.json"
            index_path.write_text(json.dumps({"blocks": metadata}), encoding="utf-8")
            vault_bridge.VAULT_INDEX_RELOAD_INTERVAL = 0
            vault_bridge._VAULT_CACHE_MAX_BYTES = 0
            vault_bridge._VAULT_CACHE_PRELOAD = 0
            index = vault_bridge.VaultIndex(str(root))
            generation_ids = []
            rss_samples = []
            for generation in range(1, 8):
                stamp = time.time() + generation + 10
                os.utime(index_path, (stamp, stamp))
                index.maybe_reload()
                generation_ids.append(index._snapshot_generation().generation_id)
                rss_samples.append(rss_kib())

            print(json.dumps({"generation_ids": generation_ids, "rss_kib": rss_samples}))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        env={**os.environ, "PYTHONHASHSEED": "0"},
        text=True,
        timeout=25,
    )
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    measured_rss_kib = payload["rss_kib"][1:]
    noise_bound_kib = 8 * 1024

    assert payload["generation_ids"] == list(range(1, 8))
    assert len(measured_rss_kib) == 6
    assert measured_rss_kib[-1] <= measured_rss_kib[0] + noise_bound_kib
    assert max(measured_rss_kib) - min(measured_rss_kib) <= noise_bound_kib
