"""P8 §3.5 — SSE first-token delay.

Isolates the parse-before-forward cost the proxy incurs on the first SSE event:
decoding and JSON-parsing the leading ``message_start`` event window via the
real ``tokenpak.proxy.streaming.iter_sse_events`` path, versus a plain utf-8
decode of the same bytes. This captures any buffering / parse-before-forward
behaviour inserted before the first token is relayed.

In-process, loopback-free measurement: it isolates the proxy's first-event parse
overhead, not the end-to-end network wall-clock from POST submit to first byte.
See each record's ``method`` field. Opt-in: ``pytest -m p8_latency``.
"""

from __future__ import annotations

import json

import pytest

from .p8_latency_harness import requires_p8_optin, run_difference_target

pytestmark = pytest.mark.p8_latency


def _first_event_bytes() -> bytes:
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
    return ("event: message_start\ndata: " + json.dumps(msg_start) + "\n\n").encode("utf-8")


def test_sse_first_token_overhead(request):
    requires_p8_optin(request)
    streaming = pytest.importorskip(
        "tokenpak.proxy.streaming", reason="proxy streaming unavailable"
    )
    iter_sse_events = streaming.iter_sse_events

    first_bytes = _first_event_bytes()

    def target() -> None:
        # Parse-before-forward of the first SSE event.
        for _ in iter_sse_events(first_bytes):
            break

    def baseline() -> None:
        _ = first_bytes.decode("utf-8", errors="replace")

    record = run_difference_target(
        target="sse_first_token",
        method=(
            "real iter_sse_events() parse of the first message_start event vs "
            "utf-8 decode of the same bytes; loopback-free parse-before-forward "
            "overhead (methodology §3.5)"
        ),
        target_fn=target,
        baseline_fn=baseline,
        default_samples=200,
        default_warmup=30,
    )

    assert record["target"] == "sse_first_token"
    assert record["sample_size"] > 0
    assert record["status"] == "measured"
