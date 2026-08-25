# SPDX-License-Identifier: Apache-2.0
"""Regression coverage for three non-blocking findings carried forward from
the prior calibrated-forecast review, into the first touch of these surfaces:

  1. ``walk_forward_coverage`` declares a ``target`` parameter it never
     reads, and coerces any non-None ``score_tail`` to RECENT_WINDOW tail
     semantics regardless of its actual value — a misleading exported
     signature.
  2. ``_participants_signature`` omits the intra-session per-turn cost
     distribution, so two sessions with identical (model, effort,
     ended_at, turns, total) but different turn-by-turn shapes collide in
     the readiness cache.
  3. ``_corpus_for`` reads the ledger fingerprint and parses the corpus as
     two separate statements on the same connection with no explicit
     transaction — a TOCTOU window where a concurrent append between the
     two reads leaves the returned corpus inconsistent with the cached
     fingerprint key.

No other behavior is touched.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from tokenpak.proxy import session_forecast_calibration as cal

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# F-API-1: walk_forward_coverage ignores `target`
# ---------------------------------------------------------------------------


def _cell(n=40, seed=5):
    import math
    import random

    rng = random.Random(seed)
    out = []
    for i in range(n):
        turns = rng.randint(6, 16)
        costs = tuple(max(200.0, rng.lognormvariate(math.log(4000.0), 0.6)) for _ in range(turns))
        out.append(
            cal.HistorySession(
                model="model-a",
                effort="unknown",
                ended_at=NOW - timedelta(hours=n - i),
                turn_costs=costs,
            )
        )
    return out


def test_wrong_target_for_one_sided_path_is_rejected():
    """one_sided measures TARGET_90 — calling it with TARGET_50 must not
    silently return the 90% coverage number under a 50% label."""
    cell = _cell()
    with pytest.raises(ValueError):
        cal.walk_forward_coverage(cell, [], cal.TARGET_50, one_sided=True)


def test_wrong_target_for_central_path_is_rejected():
    cell = _cell()
    with pytest.raises(ValueError):
        cal.walk_forward_coverage(cell, [], cal.TARGET_90)


def test_score_tail_must_equal_recent_window_exactly():
    """Any non-None score_tail currently coerces to RECENT_WINDOW tail
    semantics regardless of its value — an arbitrary int must be rejected,
    not silently treated as RECENT_WINDOW."""
    cell = _cell()
    with pytest.raises(ValueError):
        cal.walk_forward_coverage(cell, [], cal.TARGET_50, score_tail=1)


def test_score_tail_with_wrong_target_is_rejected():
    cell = _cell()
    with pytest.raises(ValueError):
        cal.walk_forward_coverage(cell, [], cal.TARGET_90, score_tail=cal.RECENT_WINDOW)


def test_correctly_labeled_calls_still_succeed():
    """The three call shapes every existing caller actually uses remain
    valid and return the same measurements as before."""
    cell = _cell()
    central = cal.walk_forward_coverage(cell, [], cal.TARGET_50)
    tail = cal.walk_forward_coverage(cell, [], cal.TARGET_50, score_tail=cal.RECENT_WINDOW)
    one_sided = cal.walk_forward_coverage(cell, [], cal.TARGET_90, one_sided=True)
    assert central[1] >= 0
    assert tail[1] >= 0
    assert one_sided[1] >= 0


# ---------------------------------------------------------------------------
# F-SIG-1: _participants_signature omits turn_costs
# ---------------------------------------------------------------------------


def test_signature_distinguishes_sessions_with_different_turn_cost_shapes():
    """Two sessions sharing (model, effort, ended_at, turns, total) but with
    a different intra-session cost distribution must NOT collide in the
    readiness cache — the signature must be sensitive to turn_costs."""
    ended_at = NOW - timedelta(hours=1)
    total = 4000.0
    turns = 4
    flat = cal.HistorySession(
        model="model-a",
        effort="unknown",
        ended_at=ended_at,
        turn_costs=(total / turns,) * turns,
    )
    skewed = cal.HistorySession(
        model="model-a",
        effort="unknown",
        ended_at=ended_at,
        turn_costs=(total - 1.0 * (turns - 1), 1.0, 1.0, 1.0),
    )
    # Same (model, effort, ended_at, turns) and near-identical rounded total.
    assert flat.model == skewed.model
    assert flat.effort == skewed.effort
    assert flat.ended_at == skewed.ended_at
    assert flat.turns == skewed.turns
    assert round(flat.total, 3) == round(skewed.total, 3)

    sig_flat = cal._participants_signature([flat])
    sig_skewed = cal._participants_signature([skewed])
    assert sig_flat != sig_skewed


# ---------------------------------------------------------------------------
# F-TOCTOU-1: _corpus_for reads fingerprint and corpus as separate snapshots
# ---------------------------------------------------------------------------


def _seed(path, n, *, model="model-a"):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE requests (id INTEGER PRIMARY KEY, session_id TEXT, model TEXT,"
        " reasoning_effort TEXT, timestamp TEXT, status_code INTEGER,"
        " input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,"
        " cache_creation_tokens INTEGER)"
    )
    start = NOW - timedelta(days=2)
    for si in range(n):
        for ti in range(6):
            conn.execute(
                "INSERT INTO requests (session_id, model, reasoning_effort, timestamp,"
                " status_code, input_tokens, output_tokens, cache_read_tokens,"
                " cache_creation_tokens) VALUES (?,?,?,?,200,3000,800,0,0)",
                (
                    f"hist-{si}",
                    model,
                    "unknown",
                    (start - timedelta(hours=n - si, minutes=-ti)).isoformat(),
                ),
            )
    conn.commit()
    conn.close()


def test_corpus_for_holds_a_snapshot_lock_across_both_reads(tmp_path):
    """The fingerprint read and the corpus parse must execute inside ONE
    transaction, so the two reads can never disagree about the ledger
    state they describe.

    Proven directly and deterministically (no thread-timing race): from
    inside the parse step, a SEPARATE connection attempts to INSERT and
    COMMIT a new row with a short busy-timeout. Two separate autocommit
    SELECTs (the pre-fix shape) each release their lock the instant they
    finish, so nothing blocks that write — it is free to land in the exact
    window between the fingerprint read and the corpus parse, letting the
    cached fingerprint (pre-insert) disagree with the cached corpus
    (post-insert). Once both reads share one transaction, this connection
    holds a lock for the whole span, and the concurrent writer's commit
    must be rejected with 'database is locked'.
    """
    db = tmp_path / "monitor.db"
    _seed(db, 10)

    real_parse = cal._parse_corpus
    outcome: dict = {}

    def _parse_with_concurrent_writer(conn):
        writer = sqlite3.connect(str(db), timeout=0.2)
        try:
            writer.execute(
                "INSERT INTO requests (session_id, model, reasoning_effort,"
                " timestamp, status_code, input_tokens, output_tokens,"
                " cache_read_tokens, cache_creation_tokens)"
                " VALUES ('race-writer', 'model-a', 'unknown', ?, 200, 1, 1, 0, 0)",
                (NOW.isoformat(),),
            )
            writer.commit()
            outcome["writer_blocked"] = False
        except sqlite3.OperationalError:
            outcome["writer_blocked"] = True
        finally:
            writer.close()
        return real_parse(conn)

    cal._CORPUS_CACHE.clear()
    with mock.patch.object(cal, "_parse_corpus", _parse_with_concurrent_writer):
        cal._corpus_for(str(db))

    assert outcome["writer_blocked"], (
        "a concurrent writer must be locked out for the ENTIRE span between "
        "the fingerprint read and the corpus parse — otherwise the cached "
        "fingerprint can describe a row count the cached corpus disagrees with"
    )
