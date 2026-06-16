"""Unit tests for tokenpak.proxy.ssrm.fingerprint."""

from __future__ import annotations

import json
import os
import time

from tokenpak.proxy.ssrm.fingerprint import (
    canonicalize_user_turn,
    fingerprint_of_body,
    hash_canonical,
    record_and_count,
)


def test_fingerprint_normalization_strips_whitespace_and_dates(tmp_ssrm_dbs):
    """Identical prompts with whitespace + date jitter → identical hash."""
    body_a = {
        "messages": [{"role": "user", "content": "Hello at 2026-05-14T12:30:00Z please help"}],
    }
    body_b = {
        "messages": [{"role": "user", "content": "Hello   at 2026-05-15T15:45:11Z please help"}],
    }
    fp_a = fingerprint_of_body(body_a)
    fp_b = fingerprint_of_body(body_b)
    assert fp_a == fp_b
    assert fp_a != ""


def test_fingerprint_distinct_for_different_prompts():
    body_a = {"messages": [{"role": "user", "content": "task one"}]}
    body_b = {"messages": [{"role": "user", "content": "task two completely different"}]}
    fp_a = fingerprint_of_body(body_a)
    fp_b = fingerprint_of_body(body_b)
    assert fp_a != fp_b


def test_fingerprint_handles_list_content_format():
    """Anthropic content can be a list of {type:'text', text:'...'} parts."""
    body = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hello world"}]}
        ]
    }
    assert fingerprint_of_body(body) != ""


def test_fingerprint_empty_for_missing_user_turn():
    body = {"messages": [{"role": "assistant", "content": "no user turn here"}]}
    assert fingerprint_of_body(body) == ""


def test_record_and_count_increments_and_returns_seen_count(tmp_ssrm_dbs):
    state_db = tmp_ssrm_dbs["state_db"]
    h = hash_canonical("the same prompt")
    n1 = record_and_count("sess-1", h, state_db)
    assert n1 == 1
    n2 = record_and_count("sess-1", h, state_db)
    assert n2 == 2
    n3 = record_and_count("sess-1", h, state_db)
    assert n3 == 3
    # Different session, same prompt → independent counter
    m = record_and_count("sess-2", h, state_db)
    assert m == 1


def test_record_and_count_ttl_prune(tmp_ssrm_dbs):
    """Rows older than ttl are removed before recording."""
    state_db = tmp_ssrm_dbs["state_db"]
    h = hash_canonical("a prompt")
    # Insert a row with timestamp 10000s ago manually via the helper using a fake now
    now = time.time()
    n_old = record_and_count("sess-old", h, state_db, now=now - 10000, ttl_seconds=3600)
    assert n_old == 1
    # Now record at "current" time with the same prompt — the old row is older
    # than ttl=3600s and should be pruned, so the new count starts at 1 again.
    n_new = record_and_count("sess-old", h, state_db, now=now, ttl_seconds=3600)
    assert n_new == 1, "TTL prune should have wiped the stale row before recording"
