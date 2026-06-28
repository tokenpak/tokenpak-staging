# SPDX-License-Identifier: Apache-2.0
"""Acceptance coverage for Standard 29 §15 — multi-request concurrent
reservation (Proposal-4 Packet 1A).

Maps tests to the packet's acceptance criteria:

1. Multiple concurrent fleet requests cannot exceed the budget reservation
   policy (atomic admission under true thread concurrency).
2. Rejected reservations do not call the provider (deny → 402 block outcome,
   nothing forwarded, no hold inserted).
3. Unused reserved amount is released after settlement (reserved-vs-actual
   reconciliation recorded).
4. Abandoned reservations expire and stop counting.
5. Reuses existing spend-guard structures (CapBreach vocabulary, EVENT_TYPES
   registry, same spend_guard.db, rolling-cap budget — no parallel ledger).
6. Pessimistic interim default when max_tokens is unset:
   min(model_context_window - projected_input, 32000).
"""

from __future__ import annotations

import json
import threading

from tokenpak.proxy.spend_guard import rolling_caps as rc
from tokenpak.proxy.spend_guard.audit import EVENT_TYPES, query_recent
from tokenpak.proxy.spend_guard.contracts import TIPDirective
from tokenpak.proxy.spend_guard.orchestrator import evaluate
from tokenpak.proxy.spend_guard.policy import SpendGuardConfig, load_config
from tokenpak.proxy.spend_guard.reservation import (
    DENIED,
    INTERIM_MAX_OUTPUT_RESERVATION,
    RESERVED,
    Reservation,
    ReservationBreach,
    ReservationStore,
    pessimistic_output_reservation,
    settle_reservation,
)
from tokenpak.proxy.spend_guard.rolling_caps import CapBreach, RollingCapsConfig

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


def reserve(store: ReservationStore, *, cost=1.0, tokens_in=0, tokens_out=0,
            agent="agent-1", session="s1", the_caps=None, settled=None,
            force=False, ttl=600, now=None):
    return store.reserve(
        session_id=session,
        fleet_id="fleet-1",
        agent_id=agent,
        projected_input_tokens=tokens_in,
        reserved_output_tokens=tokens_out,
        projected_cost_usd=cost,
        settled_usage=settled if settled is not None else dict(BLANK_SETTLED),
        caps=the_caps if the_caps is not None else caps(per_fleet_max_cost_usd=5.0),
        ttl_seconds=ttl,
        force=force,
        now=now,
    )


# ---------------------------------------------------------------------------
# Acceptance 6 — pessimistic interim default (pure function)
# ---------------------------------------------------------------------------

class TestPessimisticOutputReservation:
    def test_caller_max_tokens_is_the_reservation(self):
        assert pessimistic_output_reservation(8_000, 200_000, 50_000) == 8_000

    def test_unset_with_known_context_reserves_min_of_remaining_and_ceiling(self):
        # remaining = 200k - 50k = 150k > 32k → interim ceiling binds
        assert pessimistic_output_reservation(None, 200_000, 50_000) == 32_000
        # remaining = 200k - 190k = 10k < 32k → remaining binds
        assert pessimistic_output_reservation(None, 200_000, 190_000) == 10_000

    def test_unset_with_unknown_context_uses_interim_ceiling(self):
        assert (
            pessimistic_output_reservation(None, None, 50_000)
            == INTERIM_MAX_OUTPUT_RESERVATION
        )

    def test_input_already_past_context_reserves_zero(self):
        # decide() hard-stops such a request anyway; the floor is 0, not negative.
        assert pessimistic_output_reservation(None, 200_000, 250_000) == 0

    def test_nonpositive_max_tokens_falls_through_to_default(self):
        assert pessimistic_output_reservation(0, 200_000, 50_000) == 32_000
        assert pessimistic_output_reservation(-5, None, 0) == INTERIM_MAX_OUTPUT_RESERVATION


# ---------------------------------------------------------------------------
# Acceptance 1 — concurrent admission atomicity
# ---------------------------------------------------------------------------

class TestConcurrentAdmission:
    def test_exactly_k_of_n_threads_admitted(self, tmp_path):
        """20 threads race for a budget that fits exactly 5 — never 6."""
        store = ReservationStore(str(tmp_path / "spend_guard.db"))
        fleet_cap = caps(per_fleet_max_cost_usd=5.0)
        results = []
        lock = threading.Lock()

        def worker(i):
            status, resv, breach = reserve(
                store, cost=1.0, agent=f"agent{i % 4}",
                session=f"s{i}", the_caps=fleet_cap,
            )
            with lock:
                results.append(status)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(RESERVED) == 5
        assert results.count(DENIED) == 15
        totals = store.active_totals()
        assert totals["cost_usd"] == 5.0

    def test_per_agent_dimension_checked_before_fleet(self, tmp_path):
        store = ReservationStore(str(tmp_path / "spend_guard.db"))
        both = caps(per_agent_max_cost_usd=2.0, per_fleet_max_cost_usd=100.0)
        assert reserve(store, cost=1.5, agent="agent-1", the_caps=both)[0] == RESERVED
        status, _, breach = reserve(store, cost=1.0, agent="agent-1", the_caps=both)
        assert status == DENIED
        assert breach.cap_dimension == "per_agent_cost_usd"
        # A different agent still fits — the breach was agent-scoped.
        assert reserve(store, cost=1.0, agent="cali", the_caps=both)[0] == RESERVED

    def test_settled_baseline_counts_toward_the_budget(self, tmp_path):
        """settled + reserved + this ≤ cap — the settled term is honored."""
        store = ReservationStore(str(tmp_path / "spend_guard.db"))
        settled = dict(BLANK_SETTLED, fleet_cost_usd=4.5)
        status, _, breach = reserve(
            store, cost=1.0, settled=settled,
            the_caps=caps(per_fleet_max_cost_usd=5.0),
        )
        assert status == DENIED
        assert breach.settled_used == 4.5
        assert breach.reserved_active == 0.0

    def test_token_dimension_admission(self, tmp_path):
        store = ReservationStore(str(tmp_path / "spend_guard.db"))
        tok = caps(per_fleet_max_tokens_total=2_500)
        assert reserve(store, cost=0, tokens_in=100, tokens_out=1000, the_caps=tok)[0] == RESERVED
        assert reserve(store, cost=0, tokens_in=100, tokens_out=1000, the_caps=tok)[0] == RESERVED
        status, _, breach = reserve(store, cost=0, tokens_in=100, tokens_out=1000, the_caps=tok)
        assert status == DENIED
        assert breach.cap_dimension == "per_fleet_tokens_total"


# ---------------------------------------------------------------------------
# Acceptance 2 — rejected reservations insert nothing / never forward
# ---------------------------------------------------------------------------

class TestRejectInvariants:
    def test_denied_reserve_inserts_no_row(self, tmp_path):
        store = ReservationStore(str(tmp_path / "spend_guard.db"))
        status, resv, breach = reserve(
            store, cost=10.0, the_caps=caps(per_fleet_max_cost_usd=5.0)
        )
        assert status == DENIED and resv is None and breach is not None
        assert store.active_totals() == {"cost_usd": 0.0, "tokens": 0}

    def test_forced_hold_skips_the_check_but_still_counts(self, tmp_path):
        """TIP-approved sends are never denied but their hold is visible to
        every subsequent admission."""
        store = ReservationStore(str(tmp_path / "spend_guard.db"))
        fleet_cap = caps(per_fleet_max_cost_usd=5.0)
        status, resv, _ = reserve(store, cost=10.0, the_caps=fleet_cap, force=True)
        assert status == RESERVED
        status2, _, breach = reserve(store, cost=1.0, the_caps=fleet_cap)
        assert status2 == DENIED
        assert breach.reserved_active == 10.0


# ---------------------------------------------------------------------------
# Acceptance 3 — settlement releases the unused reserve
# ---------------------------------------------------------------------------

class TestSettlement:
    def test_settle_releases_hold_and_records_actuals(self, tmp_path):
        db = str(tmp_path / "spend_guard.db")
        store = ReservationStore(db)
        _, resv, _ = reserve(store, cost=1.0, tokens_out=500)
        assert store.active_totals()["cost_usd"] == 1.0

        settled = store.settle(resv.reservation_id, actual_cost_usd=0.4, actual_tokens=120)
        assert settled is not None and settled.status == "settled"
        assert store.active_totals() == {"cost_usd": 0.0, "tokens": 0}
        row = store.get_by_id(resv.reservation_id)
        assert row.status == "settled"
        assert row.actual_cost_usd == 0.4
        assert row.actual_tokens == 120

    def test_settle_is_idempotent(self, tmp_path):
        store = ReservationStore(str(tmp_path / "spend_guard.db"))
        _, resv, _ = reserve(store, cost=1.0)
        assert store.settle(resv.reservation_id, actual_cost_usd=0.4) is not None
        assert store.settle(resv.reservation_id, actual_cost_usd=0.4) is None

    def test_settled_capacity_is_reusable(self, tmp_path):
        store = ReservationStore(str(tmp_path / "spend_guard.db"))
        fleet_cap = caps(per_fleet_max_cost_usd=5.0)
        _, first, _ = reserve(store, cost=5.0, the_caps=fleet_cap)
        assert reserve(store, cost=1.0, the_caps=fleet_cap)[0] == DENIED
        store.settle(first.reservation_id, actual_cost_usd=3.0)
        assert reserve(store, cost=1.0, the_caps=fleet_cap)[0] == RESERVED

    def test_release_frees_a_failed_send(self, tmp_path):
        store = ReservationStore(str(tmp_path / "spend_guard.db"))
        _, resv, _ = reserve(store, cost=2.0)
        assert store.release(resv.reservation_id) is True
        assert store.active_totals()["cost_usd"] == 0.0
        assert store.release(resv.reservation_id) is False  # already released

    def test_settle_reservation_convenience_audits_reconciliation(self, tmp_path):
        db = str(tmp_path / "spend_guard.db")
        store = ReservationStore(db)
        _, resv, _ = reserve(store, cost=1.0, session="sX")
        assert settle_reservation(db, resv.reservation_id, actual_cost_usd=0.4) is True
        rows = [r for r in query_recent(db) if r["event_type"] == "reservation_settled"]
        assert len(rows) == 1
        extra = json.loads(rows[0]["extra_json"])
        assert extra["released_unused_cost_usd"] == 0.6
        assert settle_reservation(db, resv.reservation_id) is False


# ---------------------------------------------------------------------------
# Acceptance 4 — abandoned reservations expire safely
# ---------------------------------------------------------------------------

class TestExpiry:
    def test_expired_hold_stops_counting(self, tmp_path):
        store = ReservationStore(str(tmp_path / "spend_guard.db"))
        reserve(store, cost=5.0, ttl=10, now=T0)
        assert store.active_totals(now=T0 + 5)["cost_usd"] == 5.0
        assert store.active_totals(now=T0 + 11)["cost_usd"] == 0.0

    def test_admission_reclaims_expired_capacity_inline(self, tmp_path):
        """reserve() expires stale holds itself — abandonment can never wedge
        admission even if nothing calls expire_old()."""
        store = ReservationStore(str(tmp_path / "spend_guard.db"))
        fleet_cap = caps(per_fleet_max_cost_usd=5.0)
        _, stale, _ = reserve(store, cost=5.0, ttl=10, the_caps=fleet_cap, now=T0)
        assert reserve(store, cost=1.0, the_caps=fleet_cap, now=T0 + 5)[0] == DENIED
        status, _, _ = reserve(store, cost=1.0, the_caps=fleet_cap, now=T0 + 11)
        assert status == RESERVED
        assert store.get_by_id(stale.reservation_id).status == "expired"

    def test_expire_old_marks_and_counts(self, tmp_path):
        store = ReservationStore(str(tmp_path / "spend_guard.db"))
        reserve(store, cost=1.0, ttl=10, now=T0)
        reserve(store, cost=1.0, ttl=10_000, now=T0)
        assert store.expire_old(now=T0 + 11) == 1


# ---------------------------------------------------------------------------
# Acceptance 5 — reuse of existing spend-guard structures
# ---------------------------------------------------------------------------

class TestStructureReuse:
    def test_breach_is_a_capbreach(self):
        assert issubclass(ReservationBreach, CapBreach)

    def test_audit_events_live_in_the_existing_registry(self):
        assert {"reservation_block", "reservation_tip_bypass",
                "reservation_settled"} <= EVENT_TYPES

    def test_same_db_file_as_the_pending_store(self, tmp_path):
        from tokenpak.proxy.spend_guard.pending import PendingStore
        db = str(tmp_path / "spend_guard.db")
        assert ReservationStore(db).path == PendingStore(db).path


# ---------------------------------------------------------------------------
# evaluate() integration — provider-not-called-on-reject, end to end
# ---------------------------------------------------------------------------

def _cfg(tmp_path, **overrides) -> SpendGuardConfig:
    base = dict(
        enabled=True,
        reservations_enabled=True,
        reservation_ttl_seconds=600,
        rolling_caps_enabled=False,  # isolate the reservation plane
        rolling_caps_per_agent_max_cost_usd=0.0,
        rolling_caps_per_agent_max_tokens_total=0,
        rolling_caps_per_agent_max_cache_read_tokens=0,
        rolling_caps_per_fleet_max_cost_usd=0.0,
        rolling_caps_per_fleet_max_tokens_total=2_500,
        rolling_caps_per_fleet_max_cache_read_tokens=0,
        audit_db_path=str(tmp_path / "spend_guard.db"),
    )
    base.update(overrides)
    return SpendGuardConfig(**base)


def _body(session_tag: str) -> bytes:
    # ~100 fresh tokens (400 chars / 4) + max_tokens 1000 → ~1100 reserved.
    return json.dumps({
        "model": "claude-opus-4-7",
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": session_tag + "x" * (400 - len(session_tag))}],
    }).encode()


def _evaluate(cfg, session: str):
    return evaluate(
        _body(session),
        "claude-opus-4-7",
        session,
        {"x-tokenpak-agent": "agent-1", "x-tokenpak-fleet": "fleet-1"},
        config=cfg,
        target_url="https://api.anthropic.com/v1/messages",
    )


class TestEvaluateIntegration:
    def test_reservations_are_default_enabled_after_settle_wiring(self, tmp_path, tmp_monitor_db):
        cfg = load_config({"spend_guard": {"audit_db_path": str(tmp_path / "spend_guard.db")}})
        assert cfg.reservations_enabled is True

    def test_explicit_config_can_still_disable_reservations(self, tmp_path, tmp_monitor_db):
        cfg = load_config(
            {
                "spend_guard": {
                    "audit_db_path": str(tmp_path / "spend_guard.db"),
                    "reservations_enabled": False,
                }
            }
        )
        assert cfg.reservations_enabled is False

    def test_env_override_can_still_disable_reservations(
        self, monkeypatch, tmp_path, tmp_monitor_db
    ):
        monkeypatch.setenv("TOKENPAK_SPEND_GUARD_RESERVATIONS_ENABLED", "0")
        cfg = load_config({"spend_guard": {"audit_db_path": str(tmp_path / "spend_guard.db")}})
        assert cfg.reservations_enabled is False

    def test_reject_returns_block_and_never_forwards(self, tmp_path, tmp_monitor_db):
        cfg = _cfg(tmp_path)
        first = _evaluate(cfg, "s1")
        second = _evaluate(cfg, "s2")
        assert first.kind == "forward"
        assert first.reservation_id is not None
        assert second.kind == "forward"

        third = _evaluate(cfg, "s3")
        assert third.kind == "block"          # provider NOT called
        assert third.body is None             # nothing to forward
        assert third.http_status == 402
        err = json.loads(third.response_body)["error"]
        assert err["type"] == "tokenpak_spend_guard_reservation_blocked"
        assert err["cap_dimension"] == "per_fleet_tokens_total"
        assert err["retryable"] is True
        events = [r["event_type"] for r in query_recent(cfg.audit_db_path)]
        assert "reservation_block" in events

    def test_settling_a_hold_unblocks_the_next_request(self, tmp_path, tmp_monitor_db):
        cfg = _cfg(tmp_path)
        first = _evaluate(cfg, "s1")
        _evaluate(cfg, "s2")
        assert _evaluate(cfg, "s3").kind == "block"
        assert settle_reservation(
            cfg.audit_db_path, first.reservation_id,
            actual_cost_usd=0.01, actual_tokens=150,
        ) is True
        assert _evaluate(cfg, "s3").kind == "forward"

    def test_concurrent_evaluate_admits_at_most_the_budget(self, tmp_path, tmp_monitor_db):
        cfg = _cfg(tmp_path)
        outcomes = []
        lock = threading.Lock()

        def worker(i):
            out = _evaluate(cfg, f"sess-{i}")
            with lock:
                outcomes.append(out.kind)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert outcomes.count("forward") == 2
        assert outcomes.count("block") == 10

    def test_disabled_flag_is_a_strict_noop(self, tmp_path, tmp_monitor_db):
        cfg = _cfg(tmp_path, reservations_enabled=False)
        for i in range(5):
            out = _evaluate(cfg, f"s{i}")
            assert out.kind == "forward"
            assert out.reservation_id is None
        assert ReservationStore(cfg.audit_db_path).active_totals()["tokens"] == 0


class TestTipApprovedHolds:
    def test_tip_approved_send_is_forced_not_denied(self, tmp_path, tmp_monitor_db):
        """Orchestrator-level parity with the rolling-cap plane: an operator-
        approved send passes regardless of budget but its hold is recorded."""
        from tokenpak.proxy.spend_guard.estimator import estimate as run_estimate
        from tokenpak.proxy.spend_guard.orchestrator import _try_reserve

        cfg = _cfg(tmp_path, rolling_caps_per_fleet_max_tokens_total=100)  # budget far too small
        est = run_estimate(_body("s1"), "claude-opus-4-7")
        tip = TIPDirective(allow_scope="once")
        rid, block = _try_reserve(
            cfg, "s1", "fleet-1", "agent-1", est, _body("s1"), tip, 200_000,
        )
        assert block is None and rid is not None
        store = ReservationStore(cfg.audit_db_path)
        assert store.active_totals()["tokens"] > 100
        # ...and an unapproved follower is denied against that hold.
        rid2, block2 = _try_reserve(
            cfg, "s2", "fleet-1", "agent-1", est, _body("s2"), None, 200_000,
        )
        assert rid2 is None and block2 is not None
        assert block2.kind == "block"
