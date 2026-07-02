"""P8 section 3.6 - full-stream overhead.

Isolates the per-chunk parse / token-accounting overhead the proxy incurs across
an entire streamed response (≥20 SSE events): the real
``tokenpak.proxy.streaming.extract_sse_tokens`` per-event accounting over the
full stream, versus a plain utf-8 decode of the same bytes. This catches the
per-chunk overhead that would not appear in a first-token-only measurement
(JSON-line parsing on each chunk, telemetry per-event accounting).

In-process, loopback-free measurement: it isolates the proxy's per-chunk parse
overhead, not the end-to-end network wall-clock to the last byte. See each
record's ``method`` field. Opt-in: ``pytest -m p8_latency``.
"""

from __future__ import annotations

import json

import pytest

from .p8_latency_harness import requires_p8_optin, run_difference_target

pytestmark = pytest.mark.p8_latency

_NUM_DELTAS = 24  # At least 20 events per methodology sections 3.5 and 3.6.


def _full_stream_bytes() -> bytes:
    parts = []
    msg_start = {
        "type": "message_start",
        "message": {
            "id": "msg_p8",
            "type": "message",
            "role": "assistant",
            "model": "claude-3-5-sonnet",
            "usage": {"input_tokens": 1000, "output_tokens": 0},
        },
    }
    parts.append("event: message_start\ndata: " + json.dumps(msg_start) + "\n\n")
    for i in range(_NUM_DELTAS):
        delta = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": f"token-{i} "},
        }
        parts.append("event: content_block_delta\ndata: " + json.dumps(delta) + "\n\n")
    msg_delta = {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": _NUM_DELTAS},
    }
    parts.append("event: message_delta\ndata: " + json.dumps(msg_delta) + "\n\n")
    parts.append('event: message_stop\ndata: {"type":"message_stop"}\n\n')
    return "".join(parts).encode("utf-8")


def test_full_stream_overhead(request):
    requires_p8_optin(request)
    streaming = pytest.importorskip(
        "tokenpak.proxy.streaming", reason="proxy streaming unavailable"
    )
    extract_sse_tokens = streaming.extract_sse_tokens

    stream_bytes = _full_stream_bytes()

    def target() -> None:
        # Per-chunk token accounting across the whole stream.
        extract_sse_tokens(stream_bytes)

    def baseline() -> None:
        _ = stream_bytes.decode("utf-8", errors="replace")

    record = run_difference_target(
        target="full_stream",
        method=(
            "real extract_sse_tokens() per-chunk accounting over a ≥20-event "
            "stream vs utf-8 decode of the same bytes; loopback-free per-chunk "
            "parse overhead (methodology section 3.6)"
        ),
        target_fn=target,
        baseline_fn=baseline,
        default_samples=200,
        default_warmup=30,
    )

    assert record["target"] == "full_stream"
    assert record["sample_size"] > 0
    assert record["status"] == "measured"
