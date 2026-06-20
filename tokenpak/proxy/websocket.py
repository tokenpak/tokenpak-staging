"""
tokenpak.proxy.websocket — WebSocket proxy handler for the TokenPak proxy.

Provides a /ws endpoint on WS_PORT (default 8767) that:
  - Accepts JSON requests over WebSocket
  - Applies TokenPak compression pipeline
  - Forwards to Anthropic upstream with streaming
  - Streams SSE chunks back as text frames

Extracted from tokenpak/runtime/proxy.py (TPK-RESTRUCTURE-008).
"""

import asyncio
import http.client
import json
import ssl
import threading
import uuid
from typing import Dict, Optional
from urllib.parse import urlparse

from tokenpak.proxy.config import (
    UPSTREAM_ROUTES,
    UPSTREAM_TIMEOUT,
    WS_MAX_CONNECTIONS,
    WS_PORT,
)
from tokenpak.proxy.upstream_retry import (
    STATUS_DETERMINISTIC,
    STATUS_TERMINAL,
    UpstreamRetryPolicy,
    write_record,
)

# ---------------------------------------------------------------------------
# Active connection tracking
# ---------------------------------------------------------------------------

_ws_active_connections: int = 0
_ws_active_connections_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

async def _ws_handler(
    websocket,
    compact_request_body,
    retry_policy: Optional[UpstreamRetryPolicy] = None,
) -> None:
    """Handle a single WebSocket connection: receive JSON, compress, proxy to Anthropic, stream back.

    Retry contract
    --------------
    - Pre-output transient failures (5xx, 429) are retried up to policy.max_retries times.
    - 429 responses honor the ``Retry-After`` response header.
    - Deterministic mode (TOKENPAK_DETERMINISTIC_MODE=1) disables all retries.
    - Once any output frame has been delivered to the WebSocket client,
      retries are forbidden.  Post-output failures instead deliver a structured
      recovery status frame and persist a redacted record via write_record().

    Args:
        websocket: The WebSocket connection object (websockets library).
        compact_request_body: Callable from runtime/proxy.py that applies the compression pipeline.
        retry_policy: Retry configuration.  If None, built from environment via UpstreamRetryPolicy.from_env().
    """
    global _ws_active_connections

    if retry_policy is None:
        retry_policy = UpstreamRetryPolicy.from_env()

    # Check path — only /ws is supported
    req_path = "/"
    try:
        req_path = websocket.request.path
    except Exception:
        pass
    if req_path != "/ws":
        await websocket.close(1008, "Not found")
        return

    # Enforce max connections
    with _ws_active_connections_lock:
        if _ws_active_connections >= WS_MAX_CONNECTIONS:
            await websocket.close(1008, "Too many connections")
            return
        _ws_active_connections += 1

    try:
        # Receive request JSON from client
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=30.0)
        except asyncio.TimeoutError:
            await websocket.close(1008, "Receive timeout")
            return

        try:
            req_data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            await websocket.close(1003, "Invalid JSON")
            return

        # Extract identifiers for recovery records
        request_id: str = req_data.get("request_id") or str(uuid.uuid4())
        tip_plan_id: Optional[str] = req_data.get("tip_plan_id")
        model: Optional[str] = req_data.get("model")

        # Force streaming
        req_data["stream"] = True
        body_bytes: bytes = json.dumps(req_data).encode()

        # Apply TokenPak compression pipeline (sync — run in thread executor)
        loop = asyncio.get_event_loop()
        try:
            compressed_body, _sent, _orig, _prot = await loop.run_in_executor(
                None, compact_request_body, body_bytes
            )
        except Exception:
            compressed_body = body_bytes

        # Resolve Anthropic upstream
        upstream_base = UPSTREAM_ROUTES.get("anthropic-messages", "https://api.anthropic.com")
        parsed_up = urlparse(upstream_base)
        upstream_host = parsed_up.netloc or "api.anthropic.com"
        upstream_path = "/v1/messages"

        # Forward headers: pass through auth headers from WS upgrade request
        fwd_headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Content-Length": str(len(compressed_body)),
            "Host": upstream_host,
            "anthropic-version": "2023-06-01",
        }
        try:
            for hname, hval in websocket.request.headers.items():
                hl = hname.lower()
                if hl in ("x-api-key", "authorization", "anthropic-version", "anthropic-beta"):
                    fwd_headers[hl] = hval
        except Exception:
            pass

        # Connect to upstream and stream SSE back (sync — run in executor)
        def _connect_upstream():
            ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(upstream_host, timeout=UPSTREAM_TIMEOUT, context=ctx)
            conn.request("POST", upstream_path, body=compressed_body, headers=fwd_headers)
            return conn, conn.getresponse()

        # Pre-output retry loop
        conn = None
        resp = None
        for attempt in range(retry_policy.max_retries + 1):
            try:
                conn, resp = await loop.run_in_executor(None, _connect_upstream)
            except Exception as exc:
                if retry_policy.should_retry(status=503, attempt=attempt, stream_started=False):
                    delay = retry_policy.retry_delay_seconds
                    await loop.run_in_executor(None, retry_policy.sleep, delay)
                    continue
                await websocket.close(1011, f"Upstream connection failed: {str(exc)[:100]}")
                return

            if resp.status < 400:
                break  # success — proceed to streaming

            # Read any error body from upstream before deciding to retry
            try:
                err_body_bytes = await loop.run_in_executor(None, resp.read)
            except Exception:
                err_body_bytes = b""

            # Build response headers dict for Retry-After parsing
            resp_headers: Dict[str, str] = {}
            try:
                for hname in ("Retry-After", "retry-after"):
                    val = resp.getheader(hname)
                    if val is not None:
                        resp_headers[hname] = val
            except Exception:
                pass

            if retry_policy.should_retry(status=resp.status, attempt=attempt, stream_started=False):
                delay = retry_policy.retry_after_seconds(resp_headers)
                await loop.run_in_executor(None, retry_policy.sleep, delay)
                conn = None
                resp = None
                continue

            # Non-retryable error — determine status type and record
            is_deterministic = resp.status in (400, 401, 403, 404, 422)
            terminal_status = STATUS_DETERMINISTIC if is_deterministic else STATUS_TERMINAL

            try:
                write_record(
                    request_id=request_id,
                    tip_plan_id=tip_plan_id,
                    endpoint=upstream_path,
                    provider="anthropic",
                    model=model,
                    headers=fwd_headers,
                    body=compressed_body,
                    stream_started=False,
                    terminal_recovery_status=terminal_status,
                    visible_continuation_required=not is_deterministic,
                )
            except Exception:
                pass

            if err_body_bytes:
                try:
                    await websocket.send(err_body_bytes.decode("utf-8", errors="replace"))
                except Exception:
                    pass
            await websocket.close(1011, f"Upstream error {resp.status}")
            return

        if resp is None:
            # All retries exhausted on connection failure
            try:
                write_record(
                    request_id=request_id,
                    tip_plan_id=tip_plan_id,
                    endpoint=upstream_path,
                    provider="anthropic",
                    model=model,
                    headers=fwd_headers,
                    body=compressed_body,
                    stream_started=False,
                    terminal_recovery_status=STATUS_TERMINAL,
                    visible_continuation_required=True,
                )
            except Exception:
                pass
            await websocket.close(1011, "Upstream connection failed after retries")
            return

        # Stream SSE chunks back as text frames
        stream_started = False
        try:
            while True:
                chunk = await loop.run_in_executor(None, resp.read, 4096)
                if not chunk:
                    break
                try:
                    await websocket.send(chunk.decode("utf-8", errors="replace"))
                    stream_started = True
                except Exception:
                    break  # client disconnected
        except Exception as exc:
            if stream_started:
                # Post-output failure: send recovery frame, persist record
                recovery_msg = json.dumps({
                    "type": "tokenpak_recovery",
                    "status": "terminal_post_output",
                    "request_id": request_id,
                    "tip_plan_id": tip_plan_id,
                    "message": "Upstream stream interrupted; use 'tokenpak codex continue --last-failed' to resume.",
                })
                try:
                    await websocket.send(recovery_msg)
                except Exception:
                    pass
                try:
                    write_record(
                        request_id=request_id,
                        tip_plan_id=tip_plan_id,
                        endpoint=upstream_path,
                        provider="anthropic",
                        model=model,
                        headers=fwd_headers,
                        body=compressed_body,
                        stream_started=True,
                        terminal_recovery_status=STATUS_TERMINAL,
                        visible_continuation_required=True,
                    )
                except Exception:
                    pass
            try:
                await websocket.close(1011, str(exc)[:123])
            except Exception:
                pass
            return

        await websocket.close(1000, "Done")

    except Exception as exc:
        try:
            await websocket.close(1011, str(exc)[:123])
        except Exception:
            pass
    finally:
        with _ws_active_connections_lock:
            _ws_active_connections -= 1


# ---------------------------------------------------------------------------
# Server startup
# ---------------------------------------------------------------------------

def start_ws_server(compact_request_body) -> "threading.Thread | None":
    """Start the asyncio WebSocket server in a daemon thread on WS_PORT.

    Args:
        compact_request_body: The compression callable from runtime/proxy.py.

    Returns:
        The daemon thread running the WS server, or None if websockets not installed.
    """
    try:
        from websockets.asyncio.server import serve as ws_serve
    except ImportError:
        print(
            "[ws] websockets library not installed — WebSocket server disabled. Run: pip install websockets>=12.0"
        )
        return None  # type: ignore[return-value]

    async def _serve() -> None:
        async def _handler(ws):
            await _ws_handler(ws, compact_request_body)

        try:
            async with ws_serve(_handler, "0.0.0.0", WS_PORT, reuse_address=True):
                print(f"[ws] TokenPak WebSocket server ready — port={WS_PORT}")
                await asyncio.Future()  # run until cancelled
        except Exception as exc:
            print(f"[ws] WebSocket server error: {exc}")

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_serve())
        except Exception:
            pass

    t = threading.Thread(target=_run, daemon=True, name="tokenpak-ws-server")
    t.start()
    return t
