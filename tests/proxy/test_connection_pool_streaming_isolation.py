# SPDX-License-Identifier: Apache-2.0
"""NCP-3A-streaming-connect Phase 2 (issue #74) — connection-pool
streaming isolation tests.

Verifies that ``ConnectionPool.stream(...)`` routes to a separate
streaming-only ``httpx.Client`` whose defaults disable HTTP/2 and
keep-alive, while ``ConnectionPool.request(...)`` continues to use
the original shared per-netloc client.
"""

from __future__ import annotations

from unittest import mock

import httpx

from tokenpak.proxy.connection_pool import ConnectionPool, PoolConfig

# ── PoolConfig defaults ───────────────────────────────────────────────


class TestPoolConfigDefaults:

    def test_streaming_http2_default_true(self):
        # Phase 2B keeps HTTP/2 enabled for streaming (M2's
        # HTTP/1.1-fallback regressed at 22:19Z 2026-04-27).
        cfg = PoolConfig()
        assert cfg.streaming_http2 is True

    def test_streaming_keepalive_default_false(self):
        # Phase 2B disables streaming keepalive — that is the
        # narrow experimental change vs main.
        cfg = PoolConfig()
        assert cfg.streaming_keepalive is False

    def test_non_streaming_http2_default_unchanged(self):
        # Existing default — must not regress.
        cfg = PoolConfig()
        assert cfg.http2 is True

    def test_non_streaming_keepalive_default_unchanged(self):
        # Existing default — must not regress.
        cfg = PoolConfig()
        assert cfg.max_keepalive_connections == 10
        assert cfg.keepalive_expiry == 30.0


# ── Env flag wiring ───────────────────────────────────────────────────


class TestPoolConfigEnvFlags:

    def test_stream_http2_env_one_keeps_default_on(self, monkeypatch):
        monkeypatch.setenv("TOKENPAK_STREAM_HTTP2", "1")
        cfg = PoolConfig.from_env()
        assert cfg.streaming_http2 is True

    def test_stream_keepalive_env_re_enables(self, monkeypatch):
        monkeypatch.setenv("TOKENPAK_STREAM_KEEPALIVE", "1")
        cfg = PoolConfig.from_env()
        assert cfg.streaming_keepalive is True

    def test_stream_http2_env_zero_disables(self, monkeypatch):
        # Escape hatch — operator can roll back to HTTP/1.1 if
        # needed (e.g., to reproduce Phase 2 M2 conditions).
        monkeypatch.setenv("TOKENPAK_STREAM_HTTP2", "0")
        cfg = PoolConfig.from_env()
        assert cfg.streaming_http2 is False

    def test_stream_keepalive_env_zero_keeps_default_off(self, monkeypatch):
        monkeypatch.setenv("TOKENPAK_STREAM_KEEPALIVE", "0")
        cfg = PoolConfig.from_env()
        assert cfg.streaming_keepalive is False

    def test_stream_env_unset_uses_phase_2b_defaults(self, monkeypatch):
        monkeypatch.delenv("TOKENPAK_STREAM_HTTP2", raising=False)
        monkeypatch.delenv("TOKENPAK_STREAM_KEEPALIVE", raising=False)
        cfg = PoolConfig.from_env()
        assert cfg.streaming_http2 is True
        assert cfg.streaming_keepalive is False

    def test_existing_http2_env_unaffected(self, monkeypatch):
        # Setting only the streaming flag must not disturb the
        # non-streaming HTTP/2 default.
        monkeypatch.setenv("TOKENPAK_STREAM_HTTP2", "0")
        monkeypatch.delenv("TOKENPAK_HTTP2", raising=False)
        cfg = PoolConfig.from_env()
        assert cfg.http2 is True


# ── Client maps are isolated ──────────────────────────────────────────


class TestClientMapIsolation:

    def test_streaming_and_non_streaming_clients_are_distinct(self):
        pool = ConnectionPool(PoolConfig())
        try:
            req_client = pool._get_client("api.anthropic.com")
            stream_client = pool._get_streaming_client("api.anthropic.com")
            assert req_client is not stream_client
        finally:
            pool.close()

    def test_repeated_get_returns_same_streaming_client(self):
        pool = ConnectionPool(PoolConfig())
        try:
            a = pool._get_streaming_client("api.anthropic.com")
            b = pool._get_streaming_client("api.anthropic.com")
            assert a is b
        finally:
            pool.close()

    def test_streaming_client_per_netloc(self):
        pool = ConnectionPool(PoolConfig())
        try:
            a = pool._get_streaming_client("api.anthropic.com")
            b = pool._get_streaming_client("api.openai.com")
            assert a is not b
        finally:
            pool.close()


# ── Streaming client construction honors flags ────────────────────────


class TestStreamingClientConstruction:

    def test_default_streaming_client_has_no_keepalive(self):
        pool = ConnectionPool(PoolConfig())
        try:
            client = pool._get_streaming_client("api.anthropic.com")
            # httpx stores limits on the underlying connection pool:
            # client._transport._pool._max_keepalive_connections etc.
            inner_pool = client._transport._pool
            assert inner_pool._max_keepalive_connections == 0
            assert inner_pool._keepalive_expiry == 0.0
        finally:
            pool.close()

    def test_streaming_client_uses_keepalive_when_env_set(self, monkeypatch):
        monkeypatch.setenv("TOKENPAK_STREAM_KEEPALIVE", "1")
        cfg = PoolConfig.from_env()
        pool = ConnectionPool(cfg)
        try:
            client = pool._get_streaming_client("api.anthropic.com")
            inner_pool = client._transport._pool
            assert (
                inner_pool._max_keepalive_connections
                == cfg.max_keepalive_connections
            )
            assert inner_pool._keepalive_expiry == cfg.keepalive_expiry
        finally:
            pool.close()

    def test_default_streaming_client_uses_http2(self):
        # Phase 2B keeps HTTP/2 enabled for streaming. http2=True
        # at construction → the underlying connection pool's http2
        # flag is True.
        pool = ConnectionPool(PoolConfig())
        try:
            client = pool._get_streaming_client("api.anthropic.com")
            inner_pool = client._transport._pool
            assert getattr(inner_pool, "_http2", False) is True
        finally:
            pool.close()

    def test_streaming_client_http2_env_zero_falls_back_to_http1(
        self, monkeypatch
    ):
        # Escape-hatch path: TOKENPAK_STREAM_HTTP2=0 reproduces
        # the Phase 2 M2 HTTP/1.1 streaming client (still no
        # keepalive).
        monkeypatch.setenv("TOKENPAK_STREAM_HTTP2", "0")
        cfg = PoolConfig.from_env()
        pool = ConnectionPool(cfg)
        try:
            client = pool._get_streaming_client("api.anthropic.com")
            inner_pool = client._transport._pool
            assert getattr(inner_pool, "_http2", False) is False
        finally:
            pool.close()


# ── Routing — pool.stream() vs pool.request() ────────────────────────


class TestPoolRouting:

    def test_pool_stream_populates_streaming_client_map(self):
        pool = ConnectionPool(PoolConfig())
        netloc = "api.anthropic.com"
        try:
            assert netloc not in pool._streaming_clients
            assert netloc not in pool._clients
            # We can't actually call .stream() over the network in a
            # unit test, but we can mock the underlying httpx.Client
            # to verify routing. Patch `client.stream` so it returns
            # a benign context manager and verify the streaming client
            # was used.
            ctx = pool.stream(
                "POST", f"https://{netloc}/v1/messages",
                content=b"{}", headers={"Host": netloc},
            )
            # Entering and exiting the context would require a network
            # round-trip; we only need to confirm the streaming client
            # was created.
            assert netloc in pool._streaming_clients
            # And that the non-streaming client was NOT — streaming
            # routes only to the isolated client.
            assert netloc not in pool._clients
            # Clean up the un-entered context manager.
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass
        finally:
            pool.close()

    def test_pool_request_populates_non_streaming_client_map(self):
        pool = ConnectionPool(PoolConfig())
        netloc = "api.anthropic.com"
        try:
            assert netloc not in pool._streaming_clients
            assert netloc not in pool._clients
            # Mock the httpx.Client.request method to avoid network.
            with mock.patch.object(
                httpx.Client, "request",
                return_value=mock.MagicMock(
                    http_version="HTTP/2",
                    headers={"connection": "keep-alive"},
                ),
            ):
                pool.request(
                    "POST", f"https://{netloc}/v1/messages",
                    content=b"{}", headers={"Host": netloc},
                )
            assert netloc in pool._clients
            # And that the streaming client was NOT — non-streaming
            # routes only to the request client.
            assert netloc not in pool._streaming_clients
        finally:
            pool.close()


# ── close() releases both maps ────────────────────────────────────────


class TestCloseReleasesBothMaps:

    def test_close_clears_both_client_maps(self):
        pool = ConnectionPool(PoolConfig())
        # Create a client of each kind.
        pool._get_client("api.anthropic.com")
        pool._get_streaming_client("api.anthropic.com")
        assert pool._clients
        assert pool._streaming_clients
        pool.close()
        assert pool._clients == {}
        assert pool._streaming_clients == {}

    def test_close_swallows_streaming_client_close_errors(self):
        pool = ConnectionPool(PoolConfig())
        client = pool._get_streaming_client("api.anthropic.com")
        # Force the streaming client's close to raise — close() must
        # still complete cleanly and clear the map.
        with mock.patch.object(client, "close", side_effect=RuntimeError("x")):
            pool.close()
        assert pool._streaming_clients == {}


# ── Existing public surface preserved ─────────────────────────────────


class TestPublicSurfacePreserved:

    def test_active_providers_does_not_include_streaming_only(self):
        # The existing introspection `active_providers` reflects the
        # non-streaming pool only; streaming clients are isolated by
        # design and must not leak into the public providers list.
        pool = ConnectionPool(PoolConfig())
        try:
            pool._get_streaming_client("api.anthropic.com")
            assert "api.anthropic.com" not in pool.active_providers
        finally:
            pool.close()

    def test_metrics_dict_shape_unchanged(self):
        # tokenpak status / dashboard / billing readers depend on the
        # existing keys. The streaming-pool isolation must not alter
        # the metrics() dict shape.
        pool = ConnectionPool(PoolConfig())
        try:
            keys = set(pool.metrics().keys())
            assert keys == {
                "total_requests",
                "reused_connections",
                "new_connections",
                "errors",
                "reuse_rate",
            }
        finally:
            pool.close()
