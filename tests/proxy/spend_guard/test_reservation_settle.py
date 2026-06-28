# SPDX-License-Identifier: Apache-2.0
"""Acceptance coverage for Standard 29 §15 settlement (Proposal-4 Packet 2).

Packet 1A landed reservation *admission*; this packet wires *settlement* on the
proxy response path (``server.py``): a forwarded request that carried a hold is
settled on a 2xx and released on a non-2xx / provider error. These tests pin the
``ReservationStore`` / :func:`settle_reservation` semantics the response-path
hooks call, mapped to the packet's build contract + acceptance:

* **§1 settle on response / release on failure** —
  :func:`test_settle_frees_hold_and_records_actuals`,
  :func:`test_release_on_failure_frees_hold_for_retry`.
* **§2 no double-count (convert, never sum twice)** —
  :func:`test_settle_converts_hold_no_double_count` proves a settled hold's
  budget is immediately reusable and is NOT counted both as ``reserved`` and as
  settled monitor-db truth (the prop4-1a over-block hazard is gone).
* **§5 TTL-expiry interplay (idempotent no-op, never raises)** —
  :func:`test_settle_and_release_after_ttl_expiry_are_noops`,
  :func:`test_settle_is_idempotent`,
  :func:`test_release_after_settle_is_noop`.
* **§6 audit reuse (existing event, no new taxonomy)** —
  :func:`test_settle_emits_existing_reservation_settled_event`,
  :func:`test_release_emits_no_settle_event`.

The ``server.py`` wiring (reset → capture-on-forward → settle-on-2xx →
release-on-non-2xx/exception) is a thin best-effort adapter over these
primitives; its placement guarantees the settled monitor-db row is written
BEFORE the hold is converted, so the request is never momentarily uncounted.
"""

from __future__ import annotations

from tokenpak.proxy.spend_guard.audit import EVENT_TYPES, query_recent
from tokenpak.proxy.spend_guard.reservation import (
    DENIED,
    RESERVED,
    ReservationStore,
    settle_reservation,
)
from tokenpak.proxy.spend_guard.rolling_caps import RollingCapsConfig

T0 = 1_750_000_000.0  # fixed epoch for injected-clock tests

BLANK_SETTLED = {
    "agent_cost_usd": 0.0,
    "agent_tokens_total": 0,
    "agent_cache_read_tokens": 0,
    "fleet_cost_usd": 0.0,
    "fleet_tokens_total": 0,
    "fleet_cache_read_tokens": 0,
}


def caps(**overrides) -> RollingCapsConfig:
    """All dimensions disabled unless a test engages them explicitly."""
    base = dict(
        enabled=True,
        window_seconds=3600,
        per_agent_max_cost_usd=0.0,
        per_agent_max_tokens_total=0,
        per_agent_max_cache_read_tokens=0,
        per_fleet_max_cost_usd=0.0,
        per_fleet_max_tokens_total=0,
        per_fleet_max_cache_read_tokens=0,
    )
    base.update(overrides)
    return RollingCapsConfig(**base)


def _store(tmp_path) -> tuple[ReservationStore, str]:
    """A fresh ledger plus its db path (same path settle_reservation needs)."""
    dbp = str(tmp_path / "spend_guard.db")
    return ReservationStore(dbp), dbp


def _reserve(store, *, cost, settled=None, the_caps=None, ttl=600, now=None,
             agent="a1", session="s1", fleet="f1", tokens_in=0, tokens_out=0):
    return store.reserve(
        session_id=session,
        fleet_id=fleet,
        agent_id=agent,
        projected_input_tokens=tokens_in,
        reserved_output_tokens=tokens_out,
        projected_cost_usd=cost,
        settled_usage=settled if settled is not None else dict(BLANK_SETTLED),
        caps=the_caps if the_caps is not None else caps(per_fleet_max_cost_usd=10.0),
        ttl_seconds=ttl,
        now=now,
    )


# ---------------------------------------------------------------------------
# §1 — settle on response frees the hold and records actual-vs-reserved
# ---------------------------------------------------------------------------
def test_settle_frees_hold_and_records_actuals(tmp_path):
    store, dbp = _store(tmp_path)
    status, resv, breach = _reserve(store, cost=2.0, tokens_out=500)
    assert status == RESERVED and breach is None
    assert store.active_totals()["cost_usd"] == 2.0

    assert settle_reservation(
        dbp, resv.reservation_id, actual_cost_usd=1.25, actual_tokens=900
    ) is True

    # The hold no longer counts as reserved — settled truth now lives in the
    # monitor-db plane, so the ledger must not keep holding the projection.
    assert store.active_totals()["cost_usd"] == 0.0
    row = store.get_by_id(resv.reservation_id)
    assert row is not None
    assert row.status == "settled"
    assert row.actual_cost_usd == 1.25
    assert row.actual_tokens == 900


# ---------------------------------------------------------------------------
# §2 — settling CONVERTS the hold; it is never counted twice (no double-count)
# ---------------------------------------------------------------------------
def test_settle_converts_hold_no_double_count(tmp_path):
    store, dbp = _store(tmp_path)
    cap = caps(per_fleet_max_cost_usd=10.0)

    # A reserves 6.0 against a 10.0 fleet cap.
    sA, rA, _ = _reserve(store, cost=6.0, the_caps=cap)
    assert sA == RESERVED

    # While A's hold is active a second 6.0 reserve is DENIED (6 + 6 > 10):
    # admission already accounts in-flight concurrency (prop4-1a invariant).
    sB, _, bB = _reserve(store, cost=6.0, the_caps=cap)
    assert sB == DENIED and bB is not None

    # A's forward settles for 4.0 actual. The monitor-db plane now carries that
    # 4.0 as settled truth (modelled here by the settled baseline passed in).
    assert settle_reservation(
        dbp, rA.reservation_id, actual_cost_usd=4.0, actual_tokens=10
    ) is True
    assert store.active_totals()["cost_usd"] == 0.0  # hold freed

    # A racing 6.0 request now ADMITS: 4.0 settled + 0 active + 6.0 = 10 <= 10.
    # Had settlement DOUBLE-counted (kept A's 6.0 hold AND the 4.0 settled), the
    # check would be 4 + 6 + 6 = 16 > 10 and deny — so admission here is the
    # proof that the hold was converted, not summed twice.
    settled_after = dict(BLANK_SETTLED, fleet_cost_usd=4.0)
    sB2, _, bB2 = _reserve(store, cost=6.0, settled=settled_after, the_caps=cap)
    assert sB2 == RESERVED, f"settle double-counted; B2 wrongly denied: {bB2}"


# ---------------------------------------------------------------------------
# §1 — release on provider failure frees the hold so a retry can admit
# ---------------------------------------------------------------------------
def test_release_on_failure_frees_hold_for_retry(tmp_path):
    store, _ = _store(tmp_path)
    cap = caps(per_fleet_max_cost_usd=10.0)

    sA, rA, _ = _reserve(store, cost=8.0, the_caps=cap)
    assert sA == RESERVED
    # B for 4.0 is denied while A holds 8.0 (8 + 4 > 10).
    sB, _, _ = _reserve(store, cost=4.0, the_caps=cap)
    assert sB == DENIED

    # A's forward fails before any usage accrued → release. No budget burned:
    # nothing is recorded as settled, the full hold is freed.
    assert store.release(rA.reservation_id) is True
    assert store.active_totals()["cost_usd"] == 0.0

    # The retry now admits (hold freed, nothing settled against the cap).
    sB2, _, _ = _reserve(store, cost=4.0, the_caps=cap)
    assert sB2 == RESERVED


# ---------------------------------------------------------------------------
# §5 — settle/release are idempotent (exactly-once conversion)
# ---------------------------------------------------------------------------
def test_settle_is_idempotent(tmp_path):
    store, dbp = _store(tmp_path)
    _, resv, _ = _reserve(store, cost=2.0)
    assert settle_reservation(dbp, resv.reservation_id, actual_cost_usd=1.0,
                              actual_tokens=10) is True
    # A repeat settle (e.g. a retried response handler) is a no-op.
    assert settle_reservation(dbp, resv.reservation_id, actual_cost_usd=1.0,
                              actual_tokens=10) is False


def test_release_after_settle_is_noop(tmp_path):
    store, dbp = _store(tmp_path)
    _, resv, _ = _reserve(store, cost=2.0)
    assert settle_reservation(dbp, resv.reservation_id, actual_cost_usd=1.0,
                              actual_tokens=10) is True
    # Once settled, a stray release cannot re-free or alter it.
    assert store.release(resv.reservation_id) is False
    assert store.get_by_id(resv.reservation_id).status == "settled"


def test_settle_after_release_is_noop(tmp_path):
    store, dbp = _store(tmp_path)
    _, resv, _ = _reserve(store, cost=2.0)
    assert store.release(resv.reservation_id) is True
    # Once released, a late settle cannot resurrect/settle it.
    assert settle_reservation(dbp, resv.reservation_id, actual_cost_usd=1.0,
                              actual_tokens=10) is False
    assert store.get_by_id(resv.reservation_id).status == "released"


# ---------------------------------------------------------------------------
# §5 — a settle/release arriving AFTER TTL expiry is a harmless no-op
# ---------------------------------------------------------------------------
def test_settle_and_release_after_ttl_expiry_are_noops(tmp_path):
    store, dbp = _store(tmp_path)
    # Reserve at a fixed epoch with a 1s TTL, then sweep expiry past it.
    _, resv, _ = _reserve(store, cost=2.0, ttl=1, now=T0)
    assert store.expire_old(now=T0 + 5) == 1
    assert store.get_by_id(resv.reservation_id).status == "expired"

    # Neither a late settle nor a late release raises; both report "nothing
    # active to act on" (False) — the response-path hooks rely on this so a
    # slow forward that outlived its TTL never errors out the response.
    assert settle_reservation(dbp, resv.reservation_id, actual_cost_usd=1.0,
                              actual_tokens=10) is False
    assert store.release(resv.reservation_id) is False


# ---------------------------------------------------------------------------
# §6 — settlement reuses the existing audit event; release emits none
# ---------------------------------------------------------------------------
def test_settle_emits_existing_reservation_settled_event(tmp_path):
    store, dbp = _store(tmp_path)
    _, resv, _ = _reserve(store, cost=2.0, session="sess-settle")
    assert settle_reservation(dbp, resv.reservation_id, actual_cost_usd=1.0,
                              actual_tokens=10) is True

    rows = query_recent(dbp, limit=50)
    settle_rows = [r for r in rows if r["event_type"] == "reservation_settled"]
    assert settle_rows, "settlement must emit a reservation_settled audit row"
    # No new taxonomy: the event is one prop4-1a already registered.
    assert "reservation_settled" in EVENT_TYPES


def test_release_emits_no_settle_event(tmp_path):
    store, dbp = _store(tmp_path)
    _, resv, _ = _reserve(store, cost=2.0, session="sess-release")
    assert store.release(resv.reservation_id) is True

    rows = query_recent(dbp, limit=50)
    assert not [r for r in rows if r["event_type"] == "reservation_settled"], (
        "a released (failed-forward) hold must never record settled usage"
    )
