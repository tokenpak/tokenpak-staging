from __future__ import annotations

import pytest

from tokenpak.orchestration.claimability import (
    DECISION_PRIORITY,
    REASON_REGISTRY,
    BoundedScanControls,
    ClaimContext,
    PacketSnapshot,
    WorkerSnapshot,
    build_shadow_report,
    can_claim,
    registry_as_dict,
    validate_reason_registry,
)


def _packet(**overrides) -> PacketSnapshot:
    data = {
        "packet_id": "packet_valid",
        "status": "open",
        "owner": "builder",
        "queue_owner": "builder",
        "lane": "small-build",
        "readiness": "ready",
    }
    data.update(overrides)
    return PacketSnapshot(**data)


def _worker(**overrides) -> WorkerSnapshot:
    data = {
        "worker_id": "builder",
        "enabled": True,
        "paused": False,
        "lanes": ("small-build", "test-only", "docs-only"),
        "budget_blocked": False,
        "host_available": True,
    }
    data.update(overrides)
    return WorkerSnapshot(**data)


@pytest.mark.parametrize(
    "name,packet,worker,context,expected",
    [
        (
            "valid claimable packet",
            _packet(),
            _worker(),
            ClaimContext(),
            {
                "claimable": True,
                "wakeable": True,
                "primary_reason": "claimable",
                "secondary_reasons": (),
                "remediation_owner": None,
                "safe_auto_fix": None,
            },
        ),
        (
            "malformed frontmatter",
            _packet(frontmatter_valid=False, malformed_fields=("status",)),
            _worker(),
            ClaimContext(),
            {
                "claimable": False,
                "wakeable": False,
                "primary_reason": "malformed_packet",
                "secondary_reasons": (),
                "remediation_owner": "packet_author",
                "safe_auto_fix": False,
            },
        ),
        (
            "wrong owner",
            _packet(owner="reviewer", queue_owner="reviewer"),
            _worker(),
            ClaimContext(),
            {
                "claimable": False,
                "wakeable": False,
                "primary_reason": "owner_mismatch",
                "secondary_reasons": (),
                "remediation_owner": "governance",
                "safe_auto_fix": False,
            },
        ),
        (
            "wrong queue",
            _packet(queue_owner="reviewer"),
            _worker(),
            ClaimContext(),
            {
                "claimable": False,
                "wakeable": False,
                "primary_reason": "queue_location_mismatch",
                "secondary_reasons": (),
                "remediation_owner": "governance",
                "safe_auto_fix": False,
            },
        ),
        (
            "blocked dependency",
            _packet(
                depends_on=("packet_parent",),
                unresolved_dependencies=("packet_parent",),
            ),
            _worker(),
            ClaimContext(),
            {
                "claimable": False,
                "wakeable": False,
                "primary_reason": "dependency_blocked",
                "secondary_reasons": (),
                "remediation_owner": "dependency_owner",
                "safe_auto_fix": False,
            },
        ),
        (
            "STOP_REASON present",
            _packet(status="blocked", stop_reason="awaiting-upstream"),
            _worker(),
            ClaimContext(),
            {
                "claimable": False,
                "wakeable": False,
                "primary_reason": "blocked_stop_reason",
                "secondary_reasons": (),
                "remediation_owner": "governance",
                "safe_auto_fix": False,
            },
        ),
        (
            "review needs-rework claimed by wrong worker",
            _packet(status="review", qa_decision="needs-rework", claimed_by="reviewer"),
            _worker(),
            ClaimContext(),
            {
                "claimable": False,
                "wakeable": False,
                "primary_reason": "review_needs_rework_wrong_worker",
                "secondary_reasons": (),
                "remediation_owner": "governance",
                "safe_auto_fix": False,
            },
        ),
        (
            "already claimed by another worker",
            _packet(status="in-progress", claimed_by="reviewer"),
            _worker(),
            ClaimContext(),
            {
                "claimable": False,
                "wakeable": False,
                "primary_reason": "claimed_by_other",
                "secondary_reasons": (),
                "remediation_owner": "governance",
                "safe_auto_fix": False,
            },
        ),
        (
            "stale claim conflict",
            _packet(status="in-progress", claimed_by="builder", claim_conflict=True),
            _worker(),
            ClaimContext(),
            {
                "claimable": False,
                "wakeable": False,
                "primary_reason": "claim_conflict",
                "secondary_reasons": (),
                "remediation_owner": "governance",
                "safe_auto_fix": False,
            },
        ),
        (
            "noncanonical status",
            _packet(status="in_progress"),
            _worker(),
            ClaimContext(),
            {
                "claimable": False,
                "wakeable": False,
                "primary_reason": "noncanonical_status",
                "secondary_reasons": (),
                "remediation_owner": "governance",
                "safe_auto_fix": False,
            },
        ),
        (
            "worker disabled",
            _packet(),
            _worker(enabled=False),
            ClaimContext(),
            {
                "claimable": False,
                "wakeable": False,
                "primary_reason": "worker_disabled",
                "secondary_reasons": (),
                "remediation_owner": "governance",
                "safe_auto_fix": False,
            },
        ),
        (
            "worker paused",
            _packet(),
            _worker(paused=True),
            ClaimContext(),
            {
                "claimable": False,
                "wakeable": False,
                "primary_reason": "worker_paused",
                "secondary_reasons": (),
                "remediation_owner": "governance",
                "safe_auto_fix": False,
            },
        ),
        (
            "scheduler backoff active",
            _packet(),
            _worker(),
            ClaimContext(
                scheduler_backoff_active=True,
                scheduler_backoff_reason="backoff:31m",
            ),
            {
                "claimable": True,
                "wakeable": False,
                "primary_reason": "backoff_active_with_claimable_work",
                "secondary_reasons": (),
                "remediation_owner": "runtime",
                "safe_auto_fix": False,
            },
        ),
        (
            "backoff active with otherwise claimable work",
            _packet(packet_id="packet_backoff_claimable"),
            _worker(),
            ClaimContext(
                scheduler_backoff_active=True,
                scheduler_backoff_reason="backoff:2h",
            ),
            {
                "claimable": True,
                "wakeable": False,
                "primary_reason": "backoff_active_with_claimable_work",
                "secondary_reasons": (),
                "remediation_owner": "runtime",
                "safe_auto_fix": False,
            },
        ),
        (
            "budget blocked",
            _packet(),
            _worker(budget_blocked=True),
            ClaimContext(),
            {
                "claimable": False,
                "wakeable": False,
                "primary_reason": "budget_blocked",
                "secondary_reasons": (),
                "remediation_owner": "budget_owner",
                "safe_auto_fix": False,
            },
        ),
        (
            "host unreachable",
            _packet(),
            _worker(host_available=False),
            ClaimContext(),
            {
                "claimable": False,
                "wakeable": False,
                "primary_reason": "host_unreachable",
                "secondary_reasons": (),
                "remediation_owner": "runtime",
                "safe_auto_fix": False,
            },
        ),
    ],
)
def test_golden_claimability_fixture_corpus(
    name: str,
    packet: PacketSnapshot,
    worker: WorkerSnapshot,
    context: ClaimContext,
    expected: dict,
):
    decision = can_claim(worker, packet, context)

    assert decision.claimable is expected["claimable"], name
    assert decision.wakeable is expected["wakeable"], name
    assert decision.primary_reason == expected["primary_reason"], name
    assert decision.secondary_reasons == expected["secondary_reasons"], name
    if expected["primary_reason"] in REASON_REGISTRY:
        reason = REASON_REGISTRY[expected["primary_reason"]]
        assert reason.remediation_owner == expected["remediation_owner"]
        assert reason.safe_auto_fix is expected["safe_auto_fix"]
    if not decision.claimable:
        assert decision.primary_reason != "claimable"


def test_reason_registry_has_required_metadata_and_legacy_aliases():
    validate_reason_registry()
    exported = registry_as_dict()

    assert tuple(exported) == tuple(REASON_REGISTRY)
    assert set(DECISION_PRIORITY).issubset(exported)
    for reason_id, reason in exported.items():
        assert reason["id"] == reason_id
        assert isinstance(reason["blocking"], bool)
        assert isinstance(reason["safe_auto_fix"], bool)
        assert isinstance(reason["mutation_allowed"], bool)
        assert reason["canonical_message"]
        assert reason["source_layer"]
        assert isinstance(reason["evidence_required"], list)
    assert "MALFORMED_PACKET" in exported["malformed_packet"]["legacy_aliases"]
    assert "LEGACY_STATUS" in exported["noncanonical_status"]["legacy_aliases"]
    assert "STALE_CLAIM" in exported["claim_conflict"]["legacy_aliases"]


def test_priority_trace_is_deterministic_for_multiple_blockers():
    decision = can_claim(
        _worker(paused=True),
        _packet(
            status="blocked",
            stop_reason="upstream",
            unresolved_dependencies=("packet_parent",),
        ),
        ClaimContext(),
    )

    assert decision.primary_reason == "blocked_stop_reason"
    assert decision.secondary_reasons == (
        "dependency_blocked",
        "worker_paused",
    )
    assert decision.decision_priority_trace == (
        "blocked_stop_reason",
        "dependency_blocked",
        "worker_paused",
    )


def test_shadow_report_compares_current_tool_results_and_canonical_predicate():
    rows = build_shadow_report(
        packets=(
            _packet(packet_id="packet_one"),
            _packet(packet_id="packet_two", status="blocked", stop_reason="held"),
        ),
        workers=(_worker(worker_id="builder"), _worker(worker_id="reviewer")),
        context=ClaimContext(
            legacy_results={
                "cron-precount.sh": "WORK_FOUND",
                "pick-next-task.sh": "packet_one",
                "queue-watcher.sh": "would_notify",
            }
        ),
        controls=BoundedScanControls(max_packets=2, max_workers=2, max_pairs=3),
    )

    assert len(rows) == 3
    assert rows[0].packet_id == "packet_one"
    assert rows[0].worker_id == "builder"
    assert rows[0].cron_precount_result == "WORK_FOUND"
    assert rows[0].pick_next_task_result == "packet_one"
    assert rows[0].queue_watcher_interpretation == "would_notify"
    assert rows[0].scheduler_backoff_state == "inactive"
    assert rows[0].canonical_predicate_result.primary_reason == "claimable"
    assert rows[0].would_claim is True
    assert rows[0].would_wake is True


def test_shadow_report_respects_bounds_without_host_probes_or_mutation():
    packets = tuple(_packet(packet_id=f"packet_{idx}") for idx in range(10))
    workers = tuple(_worker(worker_id=f"worker_{idx}") for idx in range(10))

    rows = build_shadow_report(
        packets,
        workers,
        controls=BoundedScanControls(
            max_packets=10,
            max_workers=10,
            max_pairs=7,
            max_host_probes=0,
            max_report_bytes=1024,
        ),
    )

    assert len(rows) == 7
    assert all(row.packet_id.startswith("packet_") for row in rows)
