"""
TokenPak Connection Pool
========================

Manages per-provider ``httpx.Client`` instances with HTTP/2 and persistent
connection reuse.  Eliminates the TCP+TLS handshake overhead incurred by the
previous per-request ``http.client.HTTPSConnection`` approach.

Architecture
------------
- One ``httpx.Client`` per provider netloc (e.g. ``api.anthropic.com``).
- Clients are created lazily and kept alive for the lifetime of the pool.
- HTTP/2 is enabled by default (requires the ``h2`` package — graceful
  fallback to HTTP/1.1 if ``h2`` is not installed).
- All access is protected by a per-pool ``threading.Lock`` so it is safe to
  share a single ``ConnectionPool`` across the threaded proxy server.

Env vars (all optional)
-----------------------
``TOKENPAK_POOL_MAX_CONNECTIONS``
    Maximum concurrent connections per provider (default: ``20``).
``TOKENPAK_POOL_MAX_KEEPALIVE``
    Maximum keep-alive connections per provider (default: ``10``).
``TOKENPAK_POOL_KEEPALIVE_EXPIRY``
    Seconds before idle keep-alive connections are evicted (default: ``30``).
``TOKENPAK_HTTP2``
    Set to ``0`` to disable HTTP/2 (default: ``1`` — enabled).

Performance impact
------------------
Without pooling:
    TCP 3-way handshake  ~10–30 ms
    TLS negotiation      ~30–50 ms
    ─────────────────────────────
    Total per request    ~50–100 ms

With pooling (after first request):
    Connection reuse     <5 ms
    HTTP/2 multiplexing  <5 ms
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Dict, Optional

import httpx

# ---------------------------------------------------------------------------
# HTTP/2 availability check
# ---------------------------------------------------------------------------


def _http2_available() -> bool:
    """Return True if the ``h2`` package is installed."""
    try:
        import h2  # noqa: F401

        return True
    except ImportError:
        return False


_H2_AVAILABLE: bool = _http2_available()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class PoolConfig:
    """
    Connection pool configuration.

    Attributes
    ----------
    max_connections : int
        Maximum total connections per provider (default: 20).
    max_keepalive_connections : int
        Maximum keep-alive connections per provider (default: 10).
    keepalive_expiry : float
        Seconds before an idle keep-alive connection is evicted (default: 30).
    connect_timeout : float
        Seconds to wait for a new TCP connection (default: 10).
    read_timeout : float
        Seconds to wait for a response (default: 300 — LLM responses can be slow).
    http2 : bool
        Enable HTTP/2 when ``h2`` is installed (default: True).
    """

    max_connections: int = 20
    max_keepalive_connections: int = 10
    keepalive_expiry: float = 30.0
    connect_timeout: float = 10.0
    read_timeout: float = 300.0
    http2: bool = True
<<<<<<< Updated upstream
<<<<<<< Updated upstream
<<<<<<< Updated upstream
    # NCP-3A-streaming-connect Phase 2B (issue #74) — narrow
    # experimental mitigation. Phase 2 M2 (HTTP/1.1 + no keepalive
    # for streams) regressed verification at 22:19Z 2026-04-27.
    # Phase 2B keeps HTTP/2 multiplexing enabled (it appears to be
    # protective) and only disables keepalive on the streaming
    # path, so each stream still benefits from H2 connection-pool
    # reuse for the duration of its handshake but does not draw
    # from the stale-keepalive cohort. Reversible via
    # TOKENPAK_STREAM_HTTP2 (default 1) and
    # TOKENPAK_STREAM_KEEPALIVE (default 0).
    streaming_http2: bool = True
=======
=======
>>>>>>> Stashed changes
=======
>>>>>>> Stashed changes
    # NCP-3A-streaming-connect Phase 2 (issue #74) — streaming-only
    # client defaults. Streaming traffic isolates from the shared
    # pool to avoid HTTP/2 multiplex collapse and stale-keepalive
    # reuse classes that produced the upstream_protocol_error
    # cohort. Reversible via TOKENPAK_STREAM_HTTP2 /
    # TOKENPAK_STREAM_KEEPALIVE env vars; default ship-on (HTTP/1.1,
    # no keepalive) per Kevin 2026-04-27.
    streaming_http2: bool = False
<<<<<<< Updated upstream
<<<<<<< Updated upstream
>>>>>>> Stashed changes
=======
>>>>>>> Stashed changes
=======
>>>>>>> Stashed changes
    streaming_keepalive: bool = False

    @classmethod
    def from_env(cls) -> "PoolConfig":
        """Build a PoolConfig from environment variables."""
        return cls(
            max_connections=int(os.environ.get("TOKENPAK_POOL_MAX_CONNECTIONS", "20")),
            max_keepalive_connections=int(os.environ.get("TOKENPAK_POOL_MAX_KEEPALIVE", "10")),
            keepalive_expiry=float(os.environ.get("TOKENPAK_POOL_KEEPALIVE_EXPIRY", "30")),
            http2=os.environ.get("TOKENPAK_HTTP2", "1") != "0",
<<<<<<< Updated upstream
<<<<<<< Updated upstream
<<<<<<< Updated upstream
            streaming_http2=os.environ.get("TOKENPAK_STREAM_HTTP2", "1") != "0",
=======
            streaming_http2=os.environ.get("TOKENPAK_STREAM_HTTP2", "0") != "0",
>>>>>>> Stashed changes
=======
            streaming_http2=os.environ.get("TOKENPAK_STREAM_HTTP2", "0") != "0",
>>>>>>> Stashed changes
=======
            streaming_http2=os.environ.get("TOKENPAK_STREAM_HTTP2", "0") != "0",
>>>>>>> Stashed changes
            streaming_keepalive=(
                os.environ.get("TOKENPAK_STREAM_KEEPALIVE", "0") != "0"
            ),
        )


# ---------------------------------------------------------------------------
# Pool metrics — lightweight counters (no deps)
# ---------------------------------------------------------------------------


@dataclass
class PoolMetrics:
    """Rolling counters for connection pool health checks."""

    total_requests: int = 0
    reused_connections: int = 0
    new_connections: int = 0
    errors: int = 0

    @property
    def reuse_rate(self) -> float:
        """Fraction of requests that reused an existing connection (0–1)."""
        if self.total_requests == 0:
            return 0.0
        return round(self.reused_connections / self.total_requests, 4)

    def to_dict(self) -> dict:
        return {
            "total_requests": self.total_requests,
            "reused_connections": self.reused_connections,
            "new_connections": self.new_connections,
            "errors": self.errors,
            "reuse_rate": self.reuse_rate,
        }


# ---------------------------------------------------------------------------
# ConnectionPool
# ---------------------------------------------------------------------------


class ConnectionPool:
    """
    Thread-safe, per-provider ``httpx.Client`` pool.

    Usage
    -----
    ::

        pool = ConnectionPool()

        # Non-streaming request
        with pool.request("POST", "https://api.anthropic.com/v1/messages",
                          content=body, headers=headers) as response:
            data = response.read()

        # Streaming request (SSE)
        with pool.stream("POST", "https://api.anthropic.com/v1/messages",
                         content=body, headers=headers) as response:
            for chunk in response.iter_bytes(chunk_size=4096):
                ...

    Lifecycle
    ---------
    Call ``pool.close()`` to release all connections (e.g. on proxy shutdown).
    """

    def __init__(self, config: Optional[PoolConfig] = None) -> None:
        self._config = config or PoolConfig.from_env()
        self._clients: Dict[str, httpx.Client] = {}
<<<<<<< Updated upstream
<<<<<<< Updated upstream
<<<<<<< Updated upstream
        # NCP-3A-streaming-connect Phase 2B (issue #74) — streaming
        # traffic uses a parallel client map isolated from the
        # request/non-streaming pool. The streaming client keeps
        # HTTP/2 enabled (protective in observed traffic) but
        # disables keepalive by default so stale connections do
        # not enter the streaming reuse path. Phase 2 M2 (full
        # HTTP/1.1 + no keepalive) was rejected after verification
        # regression on 2026-04-27; see
        # docs/internal/specs/issue-74-streaming-connect-phase-2-2026-04-27.md.
=======
=======
>>>>>>> Stashed changes
=======
>>>>>>> Stashed changes
        # NCP-3A-streaming-connect Phase 2 (issue #74) — streaming
        # traffic uses a parallel client map isolated from the
        # request/non-streaming pool. The streaming client disables
        # HTTP/2 and keepalive by default so each stream gets a
        # fresh TCP connection, eliminating the multiplex-collapse
        # and stale-reuse abort classes.
<<<<<<< Updated upstream
<<<<<<< Updated upstream
>>>>>>> Stashed changes
=======
>>>>>>> Stashed changes
=======
>>>>>>> Stashed changes
        self._streaming_clients: Dict[str, httpx.Client] = {}
        self._lock = threading.Lock()
        self._metrics = PoolMetrics()
        self._metrics_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Client management
    # ------------------------------------------------------------------

    def _make_client(self) -> httpx.Client:
        """Create a new ``httpx.Client`` with pooling and HTTP/2."""
        cfg = self._config
        use_http2 = cfg.http2 and _H2_AVAILABLE

        limits = httpx.Limits(
            max_connections=cfg.max_connections,
            max_keepalive_connections=cfg.max_keepalive_connections,
            keepalive_expiry=cfg.keepalive_expiry,
        )
        timeout = httpx.Timeout(
            connect=cfg.connect_timeout,
            read=cfg.read_timeout,
            write=cfg.read_timeout,
            pool=cfg.connect_timeout,
        )

        return httpx.Client(
            http2=use_http2,
            limits=limits,
            timeout=timeout,
            follow_redirects=False,
            verify=True,  # enforce TLS certificate validation
        )

    def _get_client(self, netloc: str) -> httpx.Client:
        """
        Return (or lazily create) the ``httpx.Client`` for *netloc*.

        Thread-safe — uses a double-checked lock pattern to avoid holding
        the global lock while constructing the client.
        """
        # Fast path — client already exists
        client = self._clients.get(netloc)
        if client is not None:
            return client

        # Slow path — create under lock
        with self._lock:
            # Re-check after acquiring lock (another thread may have created it)
            if netloc not in self._clients:
                self._clients[netloc] = self._make_client()
            return self._clients[netloc]

    def _make_streaming_client(self) -> httpx.Client:
        """Create a streaming-only ``httpx.Client``.

<<<<<<< Updated upstream
<<<<<<< Updated upstream
<<<<<<< Updated upstream
        NCP-3A-streaming-connect Phase 2B (issue #74) — keeps HTTP/2
        enabled by default (protective in observed traffic; M2's
        HTTP/1.1-fallback regressed verification at 22:19Z
        2026-04-27) and only disables keepalive on the streaming
        path. Reversible via ``TOKENPAK_STREAM_HTTP2`` (default 1)
        and ``TOKENPAK_STREAM_KEEPALIVE`` (default 0).
=======
=======
>>>>>>> Stashed changes
=======
>>>>>>> Stashed changes
        NCP-3A-streaming-connect Phase 2 (issue #74) — isolated from
        the shared pool so concurrent streams cannot share a single
        multiplexed TCP connection. By default HTTP/2 is disabled
        (eliminates GOAWAY-style multiplex collapse) and keepalive
        is zero (eliminates stale-connection reuse). Reversible via
        ``TOKENPAK_STREAM_HTTP2`` / ``TOKENPAK_STREAM_KEEPALIVE``
        env vars.
<<<<<<< Updated upstream
<<<<<<< Updated upstream
>>>>>>> Stashed changes
=======
>>>>>>> Stashed changes
=======
>>>>>>> Stashed changes
        """
        cfg = self._config
        use_http2 = cfg.streaming_http2 and _H2_AVAILABLE
        max_keepalive = (
            cfg.max_keepalive_connections if cfg.streaming_keepalive else 0
        )
        keepalive_expiry = (
            cfg.keepalive_expiry if cfg.streaming_keepalive else 0.0
        )

        limits = httpx.Limits(
            max_connections=cfg.max_connections,
            max_keepalive_connections=max_keepalive,
            keepalive_expiry=keepalive_expiry,
        )
        timeout = httpx.Timeout(
            connect=cfg.connect_timeout,
            read=cfg.read_timeout,
            write=cfg.read_timeout,
            pool=cfg.connect_timeout,
        )

        return httpx.Client(
            http2=use_http2,
            limits=limits,
            timeout=timeout,
            follow_redirects=False,
            verify=True,
        )

    def _get_streaming_client(self, netloc: str) -> httpx.Client:
        """Return (or lazily create) the streaming-only client for
        *netloc*. Parallel structure to :meth:`_get_client` but uses
        the isolated streaming client map."""
        client = self._streaming_clients.get(netloc)
        if client is not None:
            return client

        with self._lock:
            if netloc not in self._streaming_clients:
                self._streaming_clients[netloc] = self._make_streaming_client()
            return self._streaming_clients[netloc]

    # ------------------------------------------------------------------
    # Public request interface
    # ------------------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        content: Optional[bytes] = None,
        headers: Optional[dict] = None,
    ) -> httpx.Response:
        """
        Send a non-streaming HTTP request via the pool.

        Returns the full ``httpx.Response`` with ``.content`` already read.
        Unlike the streaming variant, the caller does NOT need a context manager.

        Parameters
        ----------
        method : str
            HTTP method (``"GET"``, ``"POST"``, etc.).
        url : str
            Full URL including scheme.
        content : bytes, optional
            Request body.
        headers : dict, optional
            Request headers.  **Must** include ``Host`` and auth headers.

        Returns
        -------
        httpx.Response
            Response with body content loaded in memory.
        """
        parsed = httpx.URL(url)
        netloc = parsed.host
        client = self._get_client(netloc)

        with self._metrics_lock:
            self._metrics.total_requests += 1

        try:
            response = client.request(
                method,
                url,
                content=content,
                headers=headers,
            )
            # Track reuse heuristic: HTTP/2 always reuses; HTTP/1.1 reuses
            # when keep-alive is confirmed.
            with self._metrics_lock:
                proto = response.http_version  # "HTTP/1.1" or "HTTP/2"
                if (
                    proto == "HTTP/2"
                    or response.headers.get("connection", "").lower() == "keep-alive"
                ):
                    self._metrics.reused_connections += 1
                else:
                    self._metrics.new_connections += 1
            return response
        except Exception:
            with self._metrics_lock:
                self._metrics.errors += 1
                self._metrics.new_connections += 1
            raise

    def stream(
        self,
        method: str,
        url: str,
        *,
        content: Optional[bytes] = None,
        headers: Optional[dict] = None,
    ):
        """
        Send a streaming HTTP request via the pool.

        Returns an ``httpx`` streaming response context manager.
        The caller is responsible for using it as a context manager::

            with pool.stream("POST", url, content=body, headers=h) as resp:
                for chunk in resp.iter_bytes(chunk_size=4096):
                    ...

        Parameters
        ----------
        method, url, content, headers
            Same as :meth:`request`.
        """
        parsed = httpx.URL(url)
        netloc = parsed.host
<<<<<<< Updated upstream
<<<<<<< Updated upstream
<<<<<<< Updated upstream
        # NCP-3A-streaming-connect Phase 2B (issue #74) — streaming
        # traffic routes to the isolated streaming client (HTTP/2
        # preserved, keepalive disabled by default).
=======
        # NCP-3A-streaming-connect Phase 2 (issue #74) — streaming
        # traffic routes to the isolated streaming client.
>>>>>>> Stashed changes
=======
        # NCP-3A-streaming-connect Phase 2 (issue #74) — streaming
        # traffic routes to the isolated streaming client.
>>>>>>> Stashed changes
=======
        # NCP-3A-streaming-connect Phase 2 (issue #74) — streaming
        # traffic routes to the isolated streaming client.
>>>>>>> Stashed changes
        client = self._get_streaming_client(netloc)

        with self._metrics_lock:
            self._metrics.total_requests += 1

        return _StreamingContext(
            client, method, url, content, headers, self._metrics, self._metrics_lock
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def http2_enabled(self) -> bool:
        """True if HTTP/2 will be used (config says yes AND h2 is installed)."""
        return self._config.http2 and _H2_AVAILABLE

    @property
    def active_providers(self) -> list:
        """List of netloc strings for which a client has been created."""
        with self._lock:
            return list(self._clients.keys())

    def metrics(self) -> dict:
        """Return a copy of the current pool metrics."""
        with self._metrics_lock:
            return self._metrics.to_dict()

    def reset_metrics(self) -> None:
        """Reset all pool counters to zero."""
        with self._metrics_lock:
            self._metrics = PoolMetrics()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close all pooled clients and release TCP/TLS resources."""
        with self._lock:
            for client in self._clients.values():
                try:
                    client.close()
                except Exception:
                    pass
            self._clients.clear()
            for client in self._streaming_clients.values():
                try:
                    client.close()
                except Exception:
                    pass
            self._streaming_clients.clear()

    def __repr__(self) -> str:
        providers = self.active_providers
        return (
            f"<ConnectionPool providers={providers} "
            f"http2={self.http2_enabled} "
            f"metrics={self._metrics.to_dict()}>"
        )


# ---------------------------------------------------------------------------
# Streaming context helper
# ---------------------------------------------------------------------------


class _StreamingContext:
    """
    Thin wrapper that tracks metrics around ``httpx.Client.stream()``.

    Implements the context manager protocol so callers can use:
    ``with pool.stream(...) as resp:``
    """

    def __init__(
        self,
        client: httpx.Client,
        method: str,
        url: str,
        content: Optional[bytes],
        headers: Optional[dict],
        metrics: PoolMetrics,
        lock: threading.Lock,
    ) -> None:
        self._ctx = client.stream(method, url, content=content, headers=headers)
        self._metrics = metrics
        self._lock = lock
        self._response: Optional[httpx.Response] = None

    def __enter__(self) -> httpx.Response:
        self._response = self._ctx.__enter__()
        with self._lock:
            proto = self._response.http_version
            if (
                proto == "HTTP/2"
                or self._response.headers.get("connection", "").lower() == "keep-alive"
            ):
                self._metrics.reused_connections += 1
            else:
                self._metrics.new_connections += 1
        return self._response

    def __exit__(self, *args) -> None:
        self._ctx.__exit__(*args)


# ---------------------------------------------------------------------------
# Module-level singleton (used by the proxy server)
# ---------------------------------------------------------------------------

_GLOBAL_POOL: Optional[ConnectionPool] = None
_GLOBAL_POOL_LOCK = threading.Lock()


def get_global_pool() -> ConnectionPool:
    """Return (or lazily create) the module-level singleton pool."""
    global _GLOBAL_POOL
    if _GLOBAL_POOL is not None:
        return _GLOBAL_POOL
    with _GLOBAL_POOL_LOCK:
        if _GLOBAL_POOL is None:
            _GLOBAL_POOL = ConnectionPool()
    return _GLOBAL_POOL


def reset_global_pool() -> None:
    """Close and discard the global pool (used in tests)."""
    global _GLOBAL_POOL
    with _GLOBAL_POOL_LOCK:
        if _GLOBAL_POOL is not None:
            _GLOBAL_POOL.close()
            _GLOBAL_POOL = None
