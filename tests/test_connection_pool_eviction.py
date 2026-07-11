"""
Tests for connection-pool client eviction on transport errors

Covers:
- request() transport error evicts the netloc client (retry gets a fresh one)
- retry loop recovers after eviction (the incident regression case)
- session-keyed client eviction
- eviction disabled via config keeps the failed client pooled
- PoolTimeout is excluded from eviction (local saturation, not a dead link)
- stale client reference never evicts the replacement client
- evicted clients are retired (kept open for in-flight grace), then closed
- streaming: connect/send failure and mid-stream failure both evict
- downstream (non-httpx) errors in the streaming body do not evict
- from_env() parses the new timeout/eviction env vars
- metrics expose evicted_clients and retired_pending_close
"""

from __future__ import annotations

import os
import threading
import time
from typing import Iterator
from unittest.mock import patch

import httpx
import pytest

from tokenpak.proxy.connection_pool import ConnectionPool, PoolConfig

URL = "http://upstream.test/v1/messages"
NETLOC = "upstream.test"


class _FlakyTransport(httpx.BaseTransport):
    """Raises a transport error for the first *fail_times* requests, then succeeds."""

    def __init__(self, fail_times: int = 1, exc_factory=None):
        self.calls = 0
        self.fail_times = fail_times
        self.exc_factory = exc_factory or (
            lambda: httpx.ReadError("[Errno 104] Connection reset by peer")
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc_factory()
        return httpx.Response(200, json={"ok": True})


class _ExplodingStream(httpx.SyncByteStream):
    """Yields one chunk, then dies like a reset upstream connection."""

    def __iter__(self) -> Iterator[bytes]:
        yield b'data: {"type":"ping"}\n\n'
        raise httpx.ReadError("connection reset mid-stream")


class _MidStreamFailTransport(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_ExplodingStream(),
            headers={"Content-Type": "text/event-stream"},
        )


def _pool_with_transport(transport: httpx.BaseTransport, **cfg_kwargs) -> ConnectionPool:
    """Pool whose clients all use *transport* (no real sockets)."""
    pool = ConnectionPool(PoolConfig(http2=False, **cfg_kwargs))
    pool._make_client = lambda: httpx.Client(transport=transport)  # type: ignore[method-assign]
    return pool


# ---------------------------------------------------------------------------
# request() path
# ---------------------------------------------------------------------------


def test_request_transport_error_evicts_client():
    transport = _FlakyTransport(fail_times=1)
    pool = _pool_with_transport(transport)
    first = pool._get_client(NETLOC)

    with pytest.raises(httpx.ReadError):
        pool.request("POST", URL, content=b"{}")

    assert NETLOC not in pool._clients, "failed client should be evicted"
    second = pool._get_client(NETLOC)
    assert second is not first
    m = pool.metrics()
    assert m["evicted_clients"] == 1
    assert m["errors"] == 1
    pool.close()


def test_retry_loop_recovers_after_eviction():
    """The incident regression: a retry after a dead-connection failure must
    reach upstream on a fresh client instead of re-failing on the same one."""
    transport = _FlakyTransport(fail_times=1)
    pool = _pool_with_transport(transport)

    resp = None
    for _attempt in range(2):
        try:
            resp = pool.request("POST", URL, content=b"{}")
            break
        except httpx.TransportError:
            continue

    assert resp is not None and resp.status_code == 200
    assert transport.calls == 2
    assert pool.metrics()["evicted_clients"] == 1
    pool.close()


def test_session_client_eviction_and_recovery():
    transport = _FlakyTransport(fail_times=1)
    pool = _pool_with_transport(transport)
    first = pool._get_session_client(NETLOC, "sess-1")

    with pytest.raises(httpx.ReadError):
        pool.request("POST", URL, content=b"{}", session_key="sess-1")

    assert (NETLOC, "sess-1") not in pool._session_clients
    second = pool._get_session_client(NETLOC, "sess-1")
    assert second is not first

    resp = pool.request("POST", URL, content=b"{}", session_key="sess-1")
    assert resp.status_code == 200
    pool.close()


def test_eviction_disabled_via_config():
    transport = _FlakyTransport(fail_times=1)
    pool = _pool_with_transport(transport, evict_on_transport_error=False)
    first = pool._get_client(NETLOC)

    with pytest.raises(httpx.ReadError):
        pool.request("POST", URL, content=b"{}")

    assert pool._clients.get(NETLOC) is first, "client must stay pooled when disabled"
    assert pool.metrics()["evicted_clients"] == 0
    pool.close()


def test_pool_timeout_is_not_evicted():
    transport = _FlakyTransport(fail_times=1, exc_factory=lambda: httpx.PoolTimeout("busy"))
    pool = _pool_with_transport(transport)
    first = pool._get_client(NETLOC)

    with pytest.raises(httpx.PoolTimeout):
        pool.request("POST", URL, content=b"{}")

    assert pool._clients.get(NETLOC) is first
    assert pool.metrics()["evicted_clients"] == 0
    pool.close()


def test_stale_reference_does_not_evict_replacement():
    transport = _FlakyTransport(fail_times=0)
    pool = _pool_with_transport(transport)
    first = pool._get_client(NETLOC)

    assert pool._evict_client(NETLOC, None, first) is True
    replacement = pool._get_client(NETLOC)
    assert replacement is not first

    # A second eviction attempt with the stale reference must be a no-op.
    assert pool._evict_client(NETLOC, None, first) is False
    assert pool._clients.get(NETLOC) is replacement
    assert pool.metrics()["evicted_clients"] == 1
    pool.close()


def test_close_cannot_miss_concurrent_client_retirement(monkeypatch):
    """Regression: shutdown and eviction cannot strand a post-close retire."""
    pool = _pool_with_transport(
        _FlakyTransport(fail_times=0),
        retire_close_grace_seconds=3600.0,
    )
    client = pool._get_client(NETLOC)
    retire_entered = threading.Event()
    allow_retire = threading.Event()
    original_retire = pool._retire

    def paused_retire(retired_client):
        retire_entered.set()
        allow_retire.wait()
        original_retire(retired_client)

    monkeypatch.setattr(pool, "_retire", paused_retire)
    evicter = threading.Thread(
        target=pool._evict_client,
        args=(NETLOC, None, client),
        daemon=True,
    )
    closer = None
    try:
        evicter.start()
        assert retire_entered.wait(timeout=1.0)

        closer = threading.Thread(target=pool.close, daemon=True)
        closer.start()
        closer.join(timeout=0.05)
        allow_retire.set()
        evicter.join(timeout=1.0)
        closer.join(timeout=1.0)
    finally:
        allow_retire.set()
        evicter.join(timeout=1.0)
        if closer is not None:
            closer.join(timeout=1.0)

    assert evicter.is_alive() is False
    assert closer is not None
    assert closer.is_alive() is False
    assert client.is_closed is True
    assert pool.metrics()["retired_pending_close"] == 0


# ---------------------------------------------------------------------------
# Retire/close lifecycle
# ---------------------------------------------------------------------------


def test_retired_client_kept_open_during_grace_then_closed():
    transport = _FlakyTransport(fail_times=1)
    pool = _pool_with_transport(transport, retire_close_grace_seconds=3600.0)
    first = pool._get_client(NETLOC)

    with pytest.raises(httpx.ReadError):
        pool.request("POST", URL, content=b"{}")

    assert first.is_closed is False, "in-flight grace: evicted client stays open"
    assert pool.metrics()["retired_pending_close"] == 1

    pool.close()
    assert first.is_closed is True
    assert pool.metrics()["retired_pending_close"] == 0


def test_retired_client_closed_after_grace_expiry():
    transport = _FlakyTransport(fail_times=2)
    pool = _pool_with_transport(transport, retire_close_grace_seconds=0.0)
    first = pool._get_client(NETLOC)

    with pytest.raises(httpx.ReadError):
        pool.request("POST", URL, content=b"{}")

    # Grace of 0: the next reap (triggered by the next eviction) closes it.
    with pytest.raises(httpx.ReadError):
        pool.request("POST", URL, content=b"{}")

    assert first.is_closed is True
    pool.close()


# ---------------------------------------------------------------------------
# Streaming path
# ---------------------------------------------------------------------------


def test_streaming_connect_failure_evicts():
    transport = _FlakyTransport(fail_times=1)
    pool = _pool_with_transport(transport)
    first = pool._get_client(NETLOC)

    with pytest.raises(httpx.ReadError):
        with pool.stream("POST", URL, content=b"{}") as resp:
            resp.read()

    assert NETLOC not in pool._clients
    m = pool.metrics()
    assert m["evicted_clients"] == 1
    assert m["errors"] == 1
    assert pool._get_client(NETLOC) is not first
    pool.close()


def test_streaming_midstream_failure_evicts():
    pool = _pool_with_transport(_MidStreamFailTransport())
    first = pool._get_client(NETLOC)

    chunks = []
    with pytest.raises(httpx.ReadError):
        with pool.stream("POST", URL, content=b"{}") as resp:
            # chunk_size smaller than the first upstream chunk so iter_bytes
            # flushes it before the mid-stream failure surfaces.
            for chunk in resp.iter_bytes(chunk_size=8):
                chunks.append(chunk)

    assert chunks, "the pre-failure chunk should have been delivered"
    assert NETLOC not in pool._clients
    m = pool.metrics()
    assert m["evicted_clients"] == 1
    assert m["errors"] == 1
    assert pool._get_client(NETLOC) is not first
    pool.close()


def test_streaming_downstream_error_does_not_evict():
    """A non-httpx error inside the with-body (e.g. the downstream client
    hanging up on us) says nothing about upstream health — no eviction."""
    transport = _FlakyTransport(fail_times=0)
    pool = _pool_with_transport(transport)
    first = pool._get_client(NETLOC)

    with pytest.raises(BrokenPipeError):
        with pool.stream("POST", URL, content=b"{}"):
            raise BrokenPipeError("downstream client went away")

    assert pool._clients.get(NETLOC) is first
    m = pool.metrics()
    assert m["evicted_clients"] == 0
    assert m["errors"] == 0
    pool.close()


# ---------------------------------------------------------------------------
# Session reaper / LRU disposal safety
# ---------------------------------------------------------------------------


def test_idle_reap_closes_zero_reference_client_promptly():
    """An idle client with no active lease must not wait for retire grace."""
    transport = _FlakyTransport(fail_times=0)
    pool = _pool_with_transport(
        transport, session_client_idle_seconds=0.0, retire_close_grace_seconds=3600.0
    )
    first = pool._get_session_client(NETLOC, "sess-old")

    # Any later checkout triggers the idle reap of sess-old.
    pool._get_session_client(NETLOC, "sess-new")

    assert (NETLOC, "sess-old") not in pool._session_clients
    assert first.is_closed is True
    assert pool.metrics()["retired_pending_close"] == 0
    pool.close()


def test_lru_eviction_closes_zero_reference_client_promptly():
    transport = _FlakyTransport(fail_times=0)
    pool = _pool_with_transport(transport, session_client_max=1, retire_close_grace_seconds=3600.0)
    first = pool._get_session_client(NETLOC, "sess-a")
    pool._get_session_client(NETLOC, "sess-b")  # cap 1 → LRU-evicts sess-a

    assert (NETLOC, "sess-a") not in pool._session_clients
    assert first.is_closed is True
    assert pool.metrics()["retired_pending_close"] == 0
    pool.close()


def test_close_returns_within_configured_bound_when_client_close_hangs():
    """Regression: pool shutdown must not inherit a stuck transport close."""
    entered = threading.Event()
    release = threading.Event()
    blocking_done = threading.Event()
    blocking_close_was_daemon = []

    class _BlockingCloseClient:
        def close(self):
            blocking_close_was_daemon.append(threading.current_thread().daemon)
            entered.set()
            release.wait(timeout=5.0)
            blocking_done.set()

    class _FastCloseClient:
        closed = False

        def close(self):
            self.closed = True

    pool = ConnectionPool(PoolConfig(http2=False, close_timeout_seconds=0.05))
    pool._clients[NETLOC] = _BlockingCloseClient()  # type: ignore[assignment]
    fast = _FastCloseClient()
    pool._clients["other.test"] = fast  # type: ignore[assignment]
    session_lock_acquired = []

    class _SessionLockProbeClient:
        def close(self):
            acquired = pool._session_lock.acquire(timeout=0.1)
            session_lock_acquired.append(acquired)
            if acquired:
                pool._session_lock.release()

    probe = _SessionLockProbeClient()
    pool._session_clients[(NETLOC, "probe")] = (probe, time.monotonic())  # type: ignore[assignment]

    try:
        started = time.monotonic()
        pool.close()
        elapsed = time.monotonic() - started

        assert entered.is_set()
        assert elapsed < 0.5
        assert fast.closed is True, "one stuck client must not starve other closes"
        assert blocking_close_was_daemon == [True]
        assert session_lock_acquired == [True]
        assert pool._clients == {}
        assert pool._lock.acquire(timeout=0.1)
        pool._lock.release()
        assert pool._session_lock.acquire(timeout=0.1)
        pool._session_lock.release()
        assert pool._retired_lock.acquire(timeout=0.1)
        pool._retired_lock.release()
    finally:
        release.set()
        assert blocking_done.wait(timeout=1.0)


def test_request_completion_restamps_session_last_used():
    transport = _FlakyTransport(fail_times=0)
    pool = _pool_with_transport(transport)
    client = pool._get_session_client(NETLOC, "sess-1")
    key = (NETLOC, "sess-1")
    pool._session_clients[key] = (client, 0.0)  # artificially ancient

    pool.request("POST", URL, content=b"{}", session_key="sess-1")
    assert pool._session_clients[key][1] > 0.0

    pool._session_clients[key] = (client, 0.0)
    with pool.stream("POST", URL, content=b"{}", session_key="sess-1") as resp:
        resp.read()
    assert pool._session_clients[key][1] > 0.0, "stream exit must re-stamp"
    pool.close()


# ---------------------------------------------------------------------------
# Config / metrics surface
# ---------------------------------------------------------------------------


def test_from_env_parses_new_vars():
    env = {
        "TOKENPAK_POOL_CONNECT_TIMEOUT": "5",
        "TOKENPAK_POOL_READ_TIMEOUT": "120",
        "TOKENPAK_POOL_EVICT_ON_TRANSPORT_ERROR": "0",
        "TOKENPAK_POOL_RETIRE_CLOSE_GRACE_SECS": "60",
        "TOKENPAK_POOL_CLOSE_TIMEOUT_SECS": "1.5",
    }
    with patch.dict(os.environ, env):
        cfg = PoolConfig.from_env()
    assert cfg.connect_timeout == 5.0
    assert cfg.read_timeout == 120.0
    assert cfg.evict_on_transport_error is False
    assert cfg.retire_close_grace_seconds == 60.0
    assert cfg.close_timeout_seconds == 1.5


def test_from_env_defaults_for_new_vars():
    saved = {
        k: os.environ.pop(k, None)
        for k in (
            "TOKENPAK_POOL_CONNECT_TIMEOUT",
            "TOKENPAK_POOL_READ_TIMEOUT",
            "TOKENPAK_POOL_EVICT_ON_TRANSPORT_ERROR",
            "TOKENPAK_POOL_RETIRE_CLOSE_GRACE_SECS",
            "TOKENPAK_POOL_CLOSE_TIMEOUT_SECS",
        )
    }
    try:
        cfg = PoolConfig.from_env()
        assert cfg.connect_timeout == 10.0
        assert cfg.read_timeout == 300.0
        assert cfg.evict_on_transport_error is True
        assert cfg.retire_close_grace_seconds == 900.0
        assert cfg.close_timeout_seconds == 1.0
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_metrics_expose_eviction_counters():
    pool = ConnectionPool(PoolConfig(http2=False))
    m = pool.metrics()
    assert m["evicted_clients"] == 0
    assert m["retired_pending_close"] == 0
    pool.close()
