"""P8 section 3.1 - pass-through overhead.

Isolates the raw proxy framing cost on a byte-preserved passthrough: building
the forwarded-header dict (``tokenpak.proxy.headers.forward_headers``) plus the
identity body passthrough the proxy performs when every feature is disabled
(no compression, no telemetry, no cache, no PAK). Reported as the difference
versus a matched dict/bytes copy, so only the proxy-added framing remains.

This is an in-process, loopback-free measurement: it captures the proxy's
per-request header/body assembly overhead, not an end-to-end network hop. See
the ``method`` field of each emitted record for the exact measured operation.
Opt-in: ``pytest -m p8_latency``.
"""

from __future__ import annotations

import pytest

from .p8_latency_harness import requires_p8_optin, run_difference_target

pytestmark = pytest.mark.p8_latency


def _canonical_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "x-api-key": "sk-ant-" + ("x" * 24),
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "messages-2023-12-15",
        "user-agent": "tokenpak-p8-harness/1.0",
        "accept": "application/json",
        "connection": "keep-alive",
        "accept-encoding": "gzip, deflate",
    }


def _canonical_body() -> bytes:
    # ~1KB anthropic `messages` payload.
    content = "Summarise the attached context. " * 28  # ~900 chars
    payload = (
        '{"model":"claude-3-5-sonnet","max_tokens":1024,'
        '"messages":[{"role":"user","content":"' + content + '"}]}'
    )
    return payload.encode("utf-8")


def test_pass_through_overhead(request):
    requires_p8_optin(request)
    headers_mod = pytest.importorskip(
        "tokenpak.proxy.headers", reason="proxy header forwarding unavailable"
    )
    forward_headers = headers_mod.forward_headers

    headers = _canonical_headers()
    body = _canonical_body()

    def target() -> None:
        fwd = forward_headers(dict(headers), "sdk", client_has_auth=False)
        fwd["Content-Length"] = str(len(body))
        _ = bytes(body)  # byte-preserved passthrough (identity copy)

    def baseline() -> None:
        _ = dict(headers)
        _ = bytes(body)

    record = run_difference_target(
        target="pass_through",
        method=(
            "in-process proxy framing: forward_headers(headers) + body byte "
            "passthrough vs dict/bytes copy; loopback-free, isolates proxy-added "
            "framing overhead (methodology section 3.1)"
        ),
        target_fn=target,
        baseline_fn=baseline,
    )

    assert record["target"] == "pass_through"
    assert record["sample_size"] > 0
    assert record["status"] == "measured"
