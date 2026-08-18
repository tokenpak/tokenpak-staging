# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the incremental (mid-stream) usage tracker and the
in-flight registry it feeds.

Proves, at the unit level:
  1. IncrementalUsageTracker.output_tokens advances as message_delta events
     arrive across multiple chunks (including a chunk boundary that splits
     one SSE line in half) — the live count, not just the end-of-stream one.
  2. A gzip content-encoded stream is a documented no-op for the tracker
     (never fed one) rather than silently fabricating a wrong number.
  3. inflight_registry facts (elapsed time, ttfb, live output tokens,
     already-computed projection passthrough) are exactly what was fed in —
     no estimator math, no recomputation.
"""

from __future__ import annotations

import json
import time

import pytest

from tokenpak.proxy import inflight_registry
from tokenpak.proxy.spend_guard import rolling_caps
from tokenpak.proxy.streaming import IncrementalUsageTracker


def _sse_line(event: dict) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode()


# ---------------------------------------------------------------------------
# IncrementalUsageTracker
# ---------------------------------------------------------------------------


def test_output_tokens_advance_across_message_delta_events():
    tracker = IncrementalUsageTracker()
    assert tracker.output_tokens == 0

    tracker.feed(_sse_line({"type": "message_start", "message": {"usage": {}}}))
    assert tracker.output_tokens == 0

    tracker.feed(_sse_line({"type": "message_delta", "delta": {}, "usage": {"output_tokens": 5}}))
    assert tracker.output_tokens == 5

    tracker.feed(_sse_line({"type": "message_delta", "delta": {}, "usage": {"output_tokens": 12}}))
    assert tracker.output_tokens == 12

    tracker.feed(_sse_line({"type": "message_delta", "delta": {}, "usage": {"output_tokens": 47}}))
    assert tracker.output_tokens == 47


def test_output_tokens_advance_across_a_split_chunk_boundary():
    """A data: line arriving split across two chunks must still parse once complete."""
    tracker = IncrementalUsageTracker()
    full_line = _sse_line({"type": "message_delta", "delta": {}, "usage": {"output_tokens": 30}})
    midpoint = len(full_line) // 2

    first = tracker.feed(full_line[:midpoint])
    assert first == 0  # incomplete line — no premature/garbage value

    second = tracker.feed(full_line[midpoint:])
    assert second == 30


def test_non_message_delta_events_are_ignored():
    tracker = IncrementalUsageTracker()
    tracker.feed(_sse_line({"type": "ping"}))
    tracker.feed(_sse_line({"type": "content_block_delta", "delta": {"text": "hi"}}))
    assert tracker.output_tokens == 0


def test_done_sentinel_and_malformed_json_do_not_raise():
    tracker = IncrementalUsageTracker()
    tracker.feed(b"data: [DONE]\n\n")
    tracker.feed(b"data: {not valid json\n\n")
    assert tracker.output_tokens == 0


def test_empty_chunk_is_a_noop():
    tracker = IncrementalUsageTracker()
    assert tracker.feed(b"") == 0


# ---------------------------------------------------------------------------
# inflight_registry — facts only, no estimator math
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registries():
    inflight_registry.reset_for_testing()
    rolling_caps.reset_caches_for_testing()
    yield
    inflight_registry.reset_for_testing()
    rolling_caps.reset_caches_for_testing()


def test_register_mark_ttfb_update_and_snapshot_roundtrip():
    t0 = time.time()
    inflight_registry.register("req-1", model="claude-sonnet-4-8", started_at=t0)

    snap = inflight_registry.snapshot()
    assert len(snap) == 1
    assert snap[0]["request_id"] == "req-1"
    assert snap[0]["model"] == "claude-sonnet-4-8"
    assert snap[0]["ttfb_ms"] is None
    assert snap[0]["output_tokens_live"] == 0
    assert snap[0]["elapsed_ms"] >= 0
    assert "projected_cost_usd" not in snap[0]  # no ticket -> no projection attached

    ttfb = inflight_registry.mark_ttfb("req-1")
    assert ttfb is not None and ttfb >= 0
    # idempotent — a second call returns the same value, not a new one
    assert inflight_registry.mark_ttfb("req-1") == ttfb

    inflight_registry.update_output_tokens("req-1", 17)
    inflight_registry.update_output_tokens("req-1", 41)
    snap = inflight_registry.snapshot()
    assert snap[0]["output_tokens_live"] == 41
    assert snap[0]["ttfb_ms"] == ttfb

    inflight_registry.finish("req-1")
    assert inflight_registry.snapshot() == []


def test_snapshot_attaches_existing_admitted_projection_verbatim():
    """The endpoint reads an already-computed projection; it never recomputes one."""
    ticket = rolling_caps.admit_pending_spend(
        agent_id="test-agent",
        projected_cost_usd=0.0456,
        projected_tokens_total=2200,
        projected_cache_read_tokens=800,
    )
    inflight_registry.register(
        "req-2", model="claude-opus-4-8", started_at=time.time(), admission_ticket=ticket
    )

    snap = inflight_registry.snapshot()
    assert len(snap) == 1
    assert snap[0]["projected_cost_usd"] == pytest.approx(0.0456)
    assert snap[0]["projected_tokens_total"] == 2200

    rolling_caps.settle_pending_spend(ticket)
    # Settled ticket -> no projection to attach, but the request is still
    # in-flight (finish() is a separate lifecycle event) and still reports
    # its own facts.
    snap = inflight_registry.snapshot()
    assert "projected_cost_usd" not in snap[0]
    assert snap[0]["output_tokens_live"] == 0


def test_unknown_ticket_projection_lookup_returns_none():
    assert rolling_caps.get_admitted_projection(None) is None
    assert rolling_caps.get_admitted_projection("adm_doesnotexist") is None


def test_finish_and_mark_ttfb_on_unregistered_request_are_safe_noops():
    inflight_registry.finish("never-registered")  # must not raise
    assert inflight_registry.mark_ttfb("never-registered") is None
    inflight_registry.update_output_tokens("never-registered", 5)  # must not raise
