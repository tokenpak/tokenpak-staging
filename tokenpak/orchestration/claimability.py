"""Shadow claimability predicate and reason-code registry.

This module is deliberately pure: callers pass packet, worker, scheduler, and
legacy-tool evidence in, and the module returns decisions. It does not read or
write queues, wake workers, normalize packets, or touch runtime state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Iterable, Mapping, Sequence

CANONICAL_STATUSES: frozenset[str] = frozenset(
    {
        "open",
        "waiting",
        "in-progress",
        "review",
        "blocked",
        "submitted",
        "completed",
        "archived",
        "withdrawn",
        "superseded",
    }
)

TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "archived", "withdrawn", "superseded"}
)

DECISION_PRIORITY: tuple[str, ...] = (
    "malformed_packet",
    "terminal_status",
    "noncanonical_status",
    "blocked_stop_reason",
    "dependency_blocked",
    "claimed_by_other",
    "claim_conflict",
    "review_needs_rework_wrong_worker",
    "owner_mismatch",
    "queue_location_mismatch",
    "ownership_fallback_location",
    "lane_gate",
    "readiness_gate",
    "worker_disabled",
    "worker_paused",
    "budget_blocked",
    "host_unreachable",
    "backoff_active_with_claimable_work",
    "scheduler_backoff",
    "unknown_blocker",
)

SOURCE_LAYERS: frozenset[str] = frozenset(
    {
        "packet_metadata",
        "status_lifecycle",
        "ownership_routing",
        "lane_authority",
        "dependency_readiness",
        "worker_state",
        "host_state",
        "budget_spend",
        "scheduler_backoff",
        "policy_gate",
        "environment",
        "unknown",
    }
)


@dataclass(frozen=True)
class ReasonCode:
    """Canonical reason metadata for a claimability decision."""

    id: str
    category: str
    severity: str
    blocking: bool
    decision_priority: int
    remediation_owner: str
    safe_auto_fix: bool
    mutation_allowed: bool
    worker_visible: bool
    dashboard_visible: bool
    canonical_message: str
    legacy_aliases: tuple[str, ...]
    source_layer: str
    scope: str
    evidence_required: tuple[str, ...]
    default_next_action: str


@dataclass(frozen=True)
class EvaluatorResult:
    """Result emitted by one composed claimability evaluator."""

    evaluator: str
    passed: bool
    reason_candidate: str | None
    evidence: tuple[str, ...] = ()
    source_layer: str = "unknown"
    confidence: str = "high"
    affects_claimable: bool = True
    affects_wakeable: bool = True


@dataclass(frozen=True)
class PacketSnapshot:
    """Read-only packet metadata needed for shadow claimability evaluation."""

    packet_id: str
    status: str
    owner: str
    queue_owner: str
    lane: str
    readiness: str = "ready"
    depends_on: tuple[str, ...] = ()
    unresolved_dependencies: tuple[str, ...] = ()
    stop_reason: str | None = None
    claimed_by: str | None = None
    claim_conflict: bool = False
    qa_decision: str | None = None
    frontmatter_valid: bool = True
    malformed_fields: tuple[str, ...] = ()
    ownership_from_location_fallback: bool = False


@dataclass(frozen=True)
class WorkerSnapshot:
    """Read-only worker state used by the shadow predicate."""

    worker_id: str
    enabled: bool = True
    paused: bool = False
    lanes: tuple[str, ...] = ()
    budget_blocked: bool = False
    host_available: bool = True


@dataclass(frozen=True)
class ClaimContext:
    """External policy state supplied to the pure predicate."""

    scheduler_backoff_active: bool = False
    scheduler_backoff_reason: str | None = None
    legacy_results: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ClaimDecision:
    """Aggregated canonical claim decision."""

    claimable: bool
    wakeable: bool
    primary_reason: str
    secondary_reasons: tuple[str, ...]
    decision_priority_trace: tuple[str, ...]
    worker_id: str
    packet_id: str
    evidence_refs: tuple[str, ...]
    evaluator_results: tuple[EvaluatorResult, ...]


@dataclass(frozen=True)
class BoundedScanControls:
    """Upper bounds for deterministic shadow report generation."""

    max_packets: int = 25
    max_workers: int = 8
    max_pairs: int = 100
    max_wall_seconds: float = 5.0
    max_host_probes: int = 0
    max_log_lines: int = 200
    max_report_bytes: int = 250_000


@dataclass(frozen=True)
class ShadowReportRow:
    """Packet x worker comparison row for shadow parity reports."""

    packet_id: str
    worker_id: str
    cron_precount_result: str
    pick_next_task_result: str
    queue_watcher_interpretation: str
    scheduler_backoff_state: str
    canonical_predicate_result: ClaimDecision
    primary_reason: str
    secondary_reasons: tuple[str, ...]
    would_wake: bool
    would_claim: bool
    claimable: bool
    wakeable: bool
    evidence_refs: tuple[str, ...]


def _reason(
    reason_id: str,
    *,
    category: str,
    source_layer: str,
    message: str,
    severity: str = "medium",
    blocking: bool = True,
    remediation_owner: str = "governance",
    safe_auto_fix: bool = False,
    mutation_allowed: bool = False,
    worker_visible: bool = True,
    dashboard_visible: bool = True,
    aliases: tuple[str, ...] = (),
    scope: str = "packet_worker_pair",
    evidence_required: tuple[str, ...] = (),
    next_action: str = "route for governed review",
) -> ReasonCode:
    if source_layer not in SOURCE_LAYERS:
        raise ValueError(f"unknown source layer for reason {reason_id}: {source_layer}")
    return ReasonCode(
        id=reason_id,
        category=category,
        severity=severity,
        blocking=blocking,
        decision_priority=DECISION_PRIORITY.index(reason_id),
        remediation_owner=remediation_owner,
        safe_auto_fix=safe_auto_fix,
        mutation_allowed=mutation_allowed,
        worker_visible=worker_visible,
        dashboard_visible=dashboard_visible,
        canonical_message=message,
        legacy_aliases=aliases,
        source_layer=source_layer,
        scope=scope,
        evidence_required=evidence_required,
        default_next_action=next_action,
    )


REASON_REGISTRY: dict[str, ReasonCode] = {
    "malformed_packet": _reason(
        "malformed_packet",
        category="packet_shape",
        source_layer="packet_metadata",
        message="Packet frontmatter is malformed or missing required fields.",
        severity="high",
        remediation_owner="packet_author",
        aliases=("MALFORMED_PACKET",),
        evidence_required=("malformed_fields",),
        next_action="repair packet shape before claim",
    ),
    "noncanonical_status": _reason(
        "noncanonical_status",
        category="status_lifecycle",
        source_layer="status_lifecycle",
        message="Packet status is outside the canonical status set.",
        remediation_owner="governance",
        aliases=("LEGACY_STATUS",),
        evidence_required=("status",),
        next_action="normalize through governed packet repair",
    ),
    "terminal_status": _reason(
        "terminal_status",
        category="status_lifecycle",
        source_layer="status_lifecycle",
        message="Packet is already terminal and must not be claimed.",
        remediation_owner="governance",
        aliases=("DONE", "ARCHIVED"),
        evidence_required=("status",),
        next_action="skip terminal packet",
    ),
    "blocked_stop_reason": _reason(
        "blocked_stop_reason",
        category="status_lifecycle",
        source_layer="status_lifecycle",
        message="Packet is blocked by a declared stop reason.",
        evidence_required=("STOP_REASON",),
        next_action="wait for blocker resolution",
    ),
    "dependency_blocked": _reason(
        "dependency_blocked",
        category="dependency",
        source_layer="dependency_readiness",
        message="One or more dependencies are unresolved.",
        remediation_owner="dependency_owner",
        evidence_required=("depends_on", "unresolved_dependencies"),
        next_action="complete dependencies first",
    ),
    "owner_mismatch": _reason(
        "owner_mismatch",
        category="ownership",
        source_layer="ownership_routing",
        message="Packet owner does not match the evaluated worker.",
        evidence_required=("owner", "worker_id"),
        next_action="route to the packet owner",
    ),
    "queue_location_mismatch": _reason(
        "queue_location_mismatch",
        category="ownership",
        source_layer="ownership_routing",
        message="Packet queue location does not match its resolved owner.",
        evidence_required=("queue_owner", "owner"),
        next_action="route through governance before claim",
    ),
    "ownership_fallback_location": _reason(
        "ownership_fallback_location",
        category="ownership",
        source_layer="ownership_routing",
        message="Ownership was inferred from queue location fallback.",
        blocking=False,
        aliases=("LOCATION_FALLBACK",),
        evidence_required=("queue_owner",),
        next_action="add explicit owner when packet is next edited",
    ),
    "lane_gate": _reason(
        "lane_gate",
        category="authority",
        source_layer="lane_authority",
        message="Worker is not authorized for the packet lane.",
        evidence_required=("lane", "worker_lanes"),
        next_action="dispatch to an authorized worker",
    ),
    "readiness_gate": _reason(
        "readiness_gate",
        category="packet_state",
        source_layer="packet_metadata",
        message="Packet readiness is not ready.",
        evidence_required=("readiness",),
        next_action="wait for readiness to become ready",
    ),
    "claimed_by_other": _reason(
        "claimed_by_other",
        category="claim_conflict",
        source_layer="worker_state",
        message="Packet is actively claimed by another worker.",
        evidence_required=("claimed_by", "worker_id"),
        next_action="skip until claim clears",
    ),
    "claim_conflict": _reason(
        "claim_conflict",
        category="claim_conflict",
        source_layer="worker_state",
        message="Packet has a stale or conflicting claim marker.",
        aliases=("STALE_CLAIM",),
        evidence_required=("claim_conflict",),
        next_action="resolve claim conflict through governance",
    ),
    "review_needs_rework_wrong_worker": _reason(
        "review_needs_rework_wrong_worker",
        category="claim_conflict",
        source_layer="status_lifecycle",
        message="Review rework is assigned to a different worker.",
        evidence_required=("qa_decision", "claimed_by", "worker_id"),
        next_action="route rework to the previous claimant",
    ),
    "worker_disabled": _reason(
        "worker_disabled",
        category="worker_state",
        source_layer="worker_state",
        message="Worker is disabled in the supplied fleet manifest state.",
        evidence_required=("worker.enabled",),
        next_action="do not claim while disabled",
    ),
    "worker_paused": _reason(
        "worker_paused",
        category="worker_state",
        source_layer="worker_state",
        message="Worker is paused.",
        evidence_required=("worker.paused",),
        next_action="wait for worker resume",
    ),
    "scheduler_backoff": _reason(
        "scheduler_backoff",
        category="scheduler",
        source_layer="scheduler_backoff",
        message="Scheduler backoff is active.",
        remediation_owner="runtime",
        evidence_required=("scheduler_backoff_state",),
        next_action="do not wake until backoff clears",
    ),
    "backoff_active_with_claimable_work": _reason(
        "backoff_active_with_claimable_work",
        category="scheduler",
        source_layer="scheduler_backoff",
        message="Work is claimable, but scheduler backoff prevents wake.",
        remediation_owner="runtime",
        aliases=("BACKOFF_WITH_WORK",),
        evidence_required=("scheduler_backoff_state", "claimable"),
        next_action="report disagreement; do not bypass scheduler",
    ),
    "budget_blocked": _reason(
        "budget_blocked",
        category="budget",
        source_layer="budget_spend",
        message="Budget or spend guard state blocks claiming.",
        remediation_owner="budget_owner",
        evidence_required=("worker.budget_blocked",),
        next_action="wait for budget relief or approval",
    ),
    "host_unreachable": _reason(
        "host_unreachable",
        category="host",
        source_layer="host_state",
        message="Worker host is unreachable.",
        remediation_owner="runtime",
        evidence_required=("worker.host_available",),
        next_action="wait for host availability",
    ),
    "unknown_blocker": _reason(
        "unknown_blocker",
        category="unknown",
        source_layer="unknown",
        message="Claimability could not be proven from supplied evidence.",
        severity="high",
        remediation_owner="governance",
        evidence_required=("evidence_gap",),
        next_action="collect stronger evidence before claim",
    ),
}


def validate_reason_registry(
    registry: Mapping[str, ReasonCode] = REASON_REGISTRY,
) -> None:
    """Fail if the mandatory reason registry is incomplete or inconsistent."""

    missing = [reason_id for reason_id in DECISION_PRIORITY if reason_id not in registry]
    if missing:
        raise ValueError(f"reason registry missing required codes: {missing}")
    for reason_id, reason in registry.items():
        if reason.id != reason_id:
            raise ValueError(f"reason key/id mismatch: {reason_id} != {reason.id}")
        if reason.source_layer not in SOURCE_LAYERS:
            raise ValueError(f"unknown source layer for {reason_id}: {reason.source_layer}")
        if reason.decision_priority != DECISION_PRIORITY.index(reason_id):
            raise ValueError(f"decision priority mismatch for {reason_id}")


def packet_readiness(packet: PacketSnapshot, _worker: WorkerSnapshot) -> EvaluatorResult:
    if not packet.frontmatter_valid:
        fields = ", ".join(packet.malformed_fields) or "frontmatter"
        return _fail(
            "packet_readiness",
            "malformed_packet",
            f"malformed:{fields}",
            "packet_metadata",
        )
    if packet.readiness != "ready":
        return _fail(
            "packet_readiness",
            "readiness_gate",
            f"readiness:{packet.readiness}",
            "packet_metadata",
        )
    return _pass("packet_readiness", "packet_metadata")


def status_lifecycle(packet: PacketSnapshot, worker: WorkerSnapshot) -> EvaluatorResult:
    if packet.status not in CANONICAL_STATUSES:
        return _fail(
            "status_lifecycle",
            "noncanonical_status",
            f"status:{packet.status}",
            "status_lifecycle",
        )
    if packet.status in TERMINAL_STATUSES:
        return _fail(
            "status_lifecycle",
            "terminal_status",
            f"status:{packet.status}",
            "status_lifecycle",
        )
    if packet.status == "blocked" and packet.stop_reason:
        return _fail(
            "status_lifecycle",
            "blocked_stop_reason",
            f"STOP_REASON:{packet.stop_reason}",
            "status_lifecycle",
        )
    if (
        packet.status == "review"
        and packet.qa_decision == "needs-rework"
        and packet.claimed_by
        and packet.claimed_by != worker.worker_id
    ):
        return _fail(
            "status_lifecycle",
            "review_needs_rework_wrong_worker",
            f"claimed_by:{packet.claimed_by}",
            "status_lifecycle",
        )
    return _pass("status_lifecycle", "status_lifecycle")


def ownership_routing(packet: PacketSnapshot, worker: WorkerSnapshot) -> EvaluatorResult:
    if packet.queue_owner and packet.owner and packet.queue_owner != packet.owner:
        return _fail(
            "ownership_routing",
            "queue_location_mismatch",
            f"queue_owner:{packet.queue_owner}",
            "ownership_routing",
        )
    if packet.owner != worker.worker_id:
        return _fail(
            "ownership_routing",
            "owner_mismatch",
            f"owner:{packet.owner}",
            "ownership_routing",
        )
    if packet.ownership_from_location_fallback:
        return EvaluatorResult(
            evaluator="ownership_routing",
            passed=True,
            reason_candidate="ownership_fallback_location",
            evidence=(f"queue_owner:{packet.queue_owner}",),
            source_layer="ownership_routing",
            confidence="medium",
            affects_claimable=False,
            affects_wakeable=False,
        )
    return _pass("ownership_routing", "ownership_routing")


def lane_authority(packet: PacketSnapshot, worker: WorkerSnapshot) -> EvaluatorResult:
    if worker.lanes and packet.lane not in worker.lanes:
        return _fail(
            "lane_authority",
            "lane_gate",
            f"lane:{packet.lane}",
            "lane_authority",
        )
    return _pass("lane_authority", "lane_authority")


def dependency_state(packet: PacketSnapshot, _worker: WorkerSnapshot) -> EvaluatorResult:
    if packet.unresolved_dependencies:
        return _fail(
            "dependency_state",
            "dependency_blocked",
            "unresolved:" + ",".join(packet.unresolved_dependencies),
            "dependency_readiness",
        )
    return _pass("dependency_state", "dependency_readiness")


def worker_eligibility(_packet: PacketSnapshot, worker: WorkerSnapshot) -> EvaluatorResult:
    if not worker.enabled:
        return _fail("worker_eligibility", "worker_disabled", "enabled:false", "worker_state")
    if worker.paused:
        return _fail("worker_eligibility", "worker_paused", "paused:true", "worker_state")
    return _pass("worker_eligibility", "worker_state")


def scheduler_policy(
    _packet: PacketSnapshot,
    _worker: WorkerSnapshot,
    context: ClaimContext,
) -> EvaluatorResult:
    if context.scheduler_backoff_active:
        return _fail(
            "scheduler_policy",
            "scheduler_backoff",
            context.scheduler_backoff_reason or "scheduler_backoff:true",
            "scheduler_backoff",
            affects_claimable=False,
            affects_wakeable=True,
        )
    return _pass("scheduler_policy", "scheduler_backoff")


def host_availability(_packet: PacketSnapshot, worker: WorkerSnapshot) -> EvaluatorResult:
    if not worker.host_available:
        return _fail(
            "host_availability",
            "host_unreachable",
            "host_available:false",
            "host_state",
        )
    return _pass("host_availability", "host_state")


def budget_state(_packet: PacketSnapshot, worker: WorkerSnapshot) -> EvaluatorResult:
    if worker.budget_blocked:
        return _fail(
            "budget_state",
            "budget_blocked",
            "budget_blocked:true",
            "budget_spend",
        )
    return _pass("budget_state", "budget_spend")


def claim_conflict_state(packet: PacketSnapshot, worker: WorkerSnapshot) -> EvaluatorResult:
    if (
        packet.status == "review"
        and packet.qa_decision == "needs-rework"
        and packet.claimed_by
        and packet.claimed_by != worker.worker_id
    ):
        return _pass("claim_conflict_state", "worker_state")
    if packet.claimed_by and packet.claimed_by != worker.worker_id:
        return _fail(
            "claim_conflict_state",
            "claimed_by_other",
            f"claimed_by:{packet.claimed_by}",
            "worker_state",
        )
    if packet.claim_conflict:
        return _fail(
            "claim_conflict_state",
            "claim_conflict",
            "claim_conflict:true",
            "worker_state",
        )
    return _pass("claim_conflict_state", "worker_state")


def can_claim(
    worker: WorkerSnapshot,
    packet: PacketSnapshot,
    context: ClaimContext | None = None,
) -> ClaimDecision:
    """Return the composed canonical claim decision for one packet x worker pair."""

    validate_reason_registry()
    ctx = context or ClaimContext()
    results = (
        packet_readiness(packet, worker),
        status_lifecycle(packet, worker),
        ownership_routing(packet, worker),
        lane_authority(packet, worker),
        dependency_state(packet, worker),
        worker_eligibility(packet, worker),
        host_availability(packet, worker),
        budget_state(packet, worker),
        claim_conflict_state(packet, worker),
        scheduler_policy(packet, worker, ctx),
    )
    claim_blockers = _blocking_reasons(results, claimable=True)
    wake_blockers = _blocking_reasons(results, wakeable=True)
    claimable = not claim_blockers
    if claimable and "scheduler_backoff" in wake_blockers:
        wake_blockers = ("backoff_active_with_claimable_work",) + tuple(
            reason for reason in wake_blockers if reason != "scheduler_backoff"
        )
    wakeable = claimable and not wake_blockers
    reason_ids = tuple(dict.fromkeys(claim_blockers + wake_blockers))
    if not reason_ids and not claimable:
        reason_ids = ("unknown_blocker",)
    primary, secondary = _prioritized(reason_ids or ("claimable",))
    evidence = tuple(
        evidence for result in results for evidence in result.evidence
    ) or (f"packet:{packet.packet_id}", f"worker:{worker.worker_id}")
    return ClaimDecision(
        claimable=claimable,
        wakeable=wakeable,
        primary_reason=primary,
        secondary_reasons=secondary,
        decision_priority_trace=tuple(
            reason for reason in DECISION_PRIORITY if reason in reason_ids
        ),
        worker_id=worker.worker_id,
        packet_id=packet.packet_id,
        evidence_refs=evidence,
        evaluator_results=results,
    )


def build_shadow_report(
    packets: Sequence[PacketSnapshot],
    workers: Sequence[WorkerSnapshot],
    context: ClaimContext | None = None,
    controls: BoundedScanControls | None = None,
) -> tuple[ShadowReportRow, ...]:
    """Build a bounded packet x worker shadow parity report."""

    ctx = context or ClaimContext()
    bounds = controls or BoundedScanControls()
    started = monotonic()
    rows: list[ShadowReportRow] = []
    for packet in list(packets)[: bounds.max_packets]:
        for worker in list(workers)[: bounds.max_workers]:
            if len(rows) >= bounds.max_pairs:
                return tuple(rows)
            if monotonic() - started > bounds.max_wall_seconds:
                return tuple(rows)
            decision = can_claim(worker, packet, ctx)
            legacy = ctx.legacy_results
            row = ShadowReportRow(
                packet_id=packet.packet_id,
                worker_id=worker.worker_id,
                cron_precount_result=legacy.get("cron-precount.sh", "not_observed"),
                pick_next_task_result=legacy.get("pick-next-task.sh", "not_observed"),
                queue_watcher_interpretation=legacy.get(
                    "queue-watcher.sh", "not_observed"
                ),
                scheduler_backoff_state=(
                    ctx.scheduler_backoff_reason
                    if ctx.scheduler_backoff_active
                    else "inactive"
                ),
                canonical_predicate_result=decision,
                primary_reason=decision.primary_reason,
                secondary_reasons=decision.secondary_reasons,
                would_wake=decision.wakeable,
                would_claim=decision.claimable,
                claimable=decision.claimable,
                wakeable=decision.wakeable,
                evidence_refs=decision.evidence_refs,
            )
            rows.append(row)
    return tuple(rows)


def registry_as_dict(
    registry: Mapping[str, ReasonCode] = REASON_REGISTRY,
) -> dict[str, dict[str, object]]:
    """Return registry metadata as JSON/YAML-friendly dictionaries."""

    validate_reason_registry(registry)
    return {
        reason_id: {
            "id": reason.id,
            "category": reason.category,
            "severity": reason.severity,
            "blocking": reason.blocking,
            "decision_priority": reason.decision_priority,
            "remediation_owner": reason.remediation_owner,
            "safe_auto_fix": reason.safe_auto_fix,
            "mutation_allowed": reason.mutation_allowed,
            "worker_visible": reason.worker_visible,
            "dashboard_visible": reason.dashboard_visible,
            "canonical_message": reason.canonical_message,
            "legacy_aliases": list(reason.legacy_aliases),
            "source_layer": reason.source_layer,
            "scope": reason.scope,
            "evidence_required": list(reason.evidence_required),
            "default_next_action": reason.default_next_action,
        }
        for reason_id, reason in registry.items()
    }


def _blocking_reasons(
    results: Iterable[EvaluatorResult],
    *,
    claimable: bool = False,
    wakeable: bool = False,
) -> tuple[str, ...]:
    reasons: list[str] = []
    for result in results:
        if result.passed or result.reason_candidate is None:
            continue
        if claimable and not result.affects_claimable:
            continue
        if wakeable and not result.affects_wakeable:
            continue
        reason = REASON_REGISTRY[result.reason_candidate]
        if reason.blocking:
            reasons.append(reason.id)
    return tuple(dict.fromkeys(reasons))


def _prioritized(reason_ids: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    if reason_ids == ("claimable",):
        return "claimable", ()
    ordered = [reason for reason in DECISION_PRIORITY if reason in reason_ids]
    if not ordered:
        ordered = ["unknown_blocker"]
    return ordered[0], tuple(ordered[1:])


def _pass(evaluator: str, source_layer: str) -> EvaluatorResult:
    return EvaluatorResult(
        evaluator=evaluator,
        passed=True,
        reason_candidate=None,
        source_layer=source_layer,
    )


def _fail(
    evaluator: str,
    reason: str,
    evidence: str,
    source_layer: str,
    *,
    affects_claimable: bool = True,
    affects_wakeable: bool = True,
) -> EvaluatorResult:
    return EvaluatorResult(
        evaluator=evaluator,
        passed=False,
        reason_candidate=reason,
        evidence=(evidence,),
        source_layer=source_layer,
        affects_claimable=affects_claimable,
        affects_wakeable=affects_wakeable,
    )


__all__ = [
    "BoundedScanControls",
    "CANONICAL_STATUSES",
    "ClaimContext",
    "ClaimDecision",
    "DECISION_PRIORITY",
    "EvaluatorResult",
    "PacketSnapshot",
    "REASON_REGISTRY",
    "ReasonCode",
    "ShadowReportRow",
    "SOURCE_LAYERS",
    "TERMINAL_STATUSES",
    "WorkerSnapshot",
    "build_shadow_report",
    "can_claim",
    "registry_as_dict",
    "validate_reason_registry",
]
