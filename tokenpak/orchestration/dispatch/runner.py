"""FulfillmentLine runner — sequential station execution.

The :class:`FulfillmentLine` is the top-level execution engine P-EXEC-01 ships. It
takes a *selected, bound* route (from :class:`DispatchRuntime.select_route`) and
runs its stations **sequentially**, one :class:`StationRunner` per station, until
the route completes, a gate blocks, a decision is required, or cancellation
propagates.

**Sequential execution only.** Stations run strictly in declaration order; the
output of each station is available to the next. There is **no parallel
execution** and **no branch primitive** in v0.1-alpha — these are a deliberate
omission (parallel fulfillment and branch decisions are explicitly NOT in
v0.1-alpha). A FulfillmentLine is a *line*, not a graph; the
runner asserts this by walking ``route.stations`` in order with no fan-out.

What the FulfillmentLine wires together:

* the **StationRunner** for each worker station (worker + overlay + context cargo
  + tool registry + bounded loop, :mod:`.station_runner`);
* the **Reviewer Station** for a station whose role is ``reviewer``,
  invoked through the injected :class:`ReviewerLLM` boundary;
* the **Gatehouse** Delivery Gate — reviewer ``pass`` continues,
  ``warning`` auto-creates a :class:`DispatchDecision`, ``fail`` blocks delivery;
* the **Run Ledger** — the :class:`DispatchRun` record is written at start and
  updated as stations complete; each :class:`DispatchStationRun` is committed by
  its StationRunner only after schema-valid output (criterion 4);
* **Spend Guard inheritance** — a station that fails with
  ``reason=spend_guard_exceeded`` halts the line and surfaces a
  :class:`DispatchDecision` (raise budget / change route / cancel);
* **Resume** — :meth:`FulfillmentLine.resume` reconciles an interrupted
  run via :func:`reconcile_run` before continuing;
* **Cancellation** — a cancel token marks queued stations ``cancelled``
  and captures a late TIP result as a :class:`LateResult`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional
from uuid import uuid4

from .gatehouse import DeliveryPackage, DeliveryStatus, Gatehouse
from .ledger.db import RunLedger
from .loop_policy import ROUTE_WALL_SECOND_DEFAULTS
from .models.artifact import DispatchArtifact
from .models.decision import (
    DecisionDefaultAction,
    DecisionOption,
    DecisionRecommendation,
    DispatchDecision,
)
from .models.effect import DispatchEffect
from .models.enums import (
    AutoApplyAfter,
    AutonomyMode,
    DecisionScope,
    DecisionStatus,
    RiskLevel,
    StationRunStatus,
)
from .models.late_result import LateResult
from .models.manifest import DispatchManifest
from .models.receipt import DispatchReceipt
from .models.route import DispatchRoute, RouteStation
from .models.run import DispatchRun
from .models.station_run import DispatchStationRun
from .models.worker import DispatchWorker
from .receipt_builder import build_and_write_receipt
from .registry.routes import is_worker_station
from .registry.workers import (
    DispatchWorkerRegistry,
    OverlayLoader,
    PromptOverlay,
    assert_route_binding,
)
from .resume import ResumeAction, ResumeOutcome, reconcile_run
from .station_runner import (
    SPEND_GUARD_EXCEEDED_REASON,
    CancelToken,
    FlagCancelToken,
    SpendGuard,
    StationRunner,
    WorkerLLM,
    unlimited_spend_guard,
)
from .stations.reviewer import (
    CommandEvidence,
    EvidenceTestResult,
    NegativeEvidence,
    ReviewerLLM,
    ReviewerStation,
    ReviewerStationInput,
    ReviewerStationResult,
)


# Status the line returns. Distinct from any single station's status: it reports
# the *line-level* outcome the caller acts on.
class LineStatus(str, Enum):
    """Outcome of a FulfillmentLine run (the caller's directive)."""

    DELIVERED = "delivered"  # all stations ran; delivery gate ready
    DELIVERY_READY_WITH_WARNING = "delivery_ready_with_warning"
    BLOCKED = "blocked"  # delivery gate blocked (reviewer fail / failed check)
    DECISION_REQUIRED = "decision_required"  # a decision halted the line
    CANCELLED = "cancelled"  # cancellation propagated
    FAILED = "failed"  # a station failed (non-spend-guard)


@dataclass
class FulfillmentResult:
    """The result of running a FulfillmentLine.

    Carries the line status, the persisted :class:`DispatchRun`, the per-station
    :class:`DispatchStationRun` records produced, any :class:`DispatchDecision`
    that halted the line (spend-guard / reviewer-warning / resume drift), the
    Gatehouse :class:`DeliveryPackage` when a delivery gate ran, and any
    :class:`LateResult` captured on cancellation.
    """

    status: LineStatus
    run: DispatchRun
    station_runs: list[DispatchStationRun] = field(default_factory=list)
    decision: Optional[DispatchDecision] = None
    delivery_package: Optional[DeliveryPackage] = None
    late_results: list[LateResult] = field(default_factory=list)
    effect_ids: list[str] = field(default_factory=list)
    reviewer_result: Optional[ReviewerStationResult] = None
    receipt: Optional[DispatchReceipt] = None
    reason: str = ""


class FulfillmentLine:
    """Sequential station-execution engine.

    Construct with the foundation seams — a :class:`WorkerLLM` (the TIP worker
    boundary), a context provider, a :class:`RunLedger`, a worker registry, and
    optional Spend Guard / cancel token / reviewer client / overlay loader. Call
    :meth:`run` with a *selected, bound* route, the manifest, and the autonomy
    mode.

    **Sequential, no parallel, no branches.** :meth:`_walk_stations` iterates
    ``route.stations`` in order. There is no fan-out, no concurrent station, and
    no conditional branch primitive — that is the deliberate v0.1-alpha omission.
    A later version may add a branch model; this runner does not.
    """

    def __init__(
        self,
        *,
        worker_llm: WorkerLLM,
        context_provider: Any,
        ledger: RunLedger,
        worker_registry: DispatchWorkerRegistry,
        reviewer_llm: Optional[ReviewerLLM] = None,
        overlay_loader: Optional[OverlayLoader] = None,
        gatehouse: Optional[Gatehouse] = None,
        spend_guard: Optional[SpendGuard] = None,
        cancel_token: Optional[CancelToken] = None,
        tool_runner: Optional[Callable[[Any], Any]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._worker_llm = worker_llm
        self._context_provider = context_provider
        self._ledger = ledger
        self._workers = worker_registry
        self._reviewer_llm = reviewer_llm
        self._overlay_loader = overlay_loader if overlay_loader is not None else OverlayLoader()
        self._gatehouse = gatehouse or Gatehouse()
        self._spend_guard = spend_guard or unlimited_spend_guard
        self._cancel = cancel_token or FlagCancelToken(False)
        self._tool_runner = tool_runner
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # -- public API ----------------------------------------------------------

    def run(
        self,
        *,
        route: DispatchRoute,
        manifest: DispatchManifest,
        autonomy_mode: AutonomyMode | str,
        route_intent: Optional[str] = None,
        run_id: Optional[str] = None,
        approval_granted: bool = False,
    ) -> FulfillmentResult:
        """Run a route's stations sequentially and return the line result.

        Writes a :class:`DispatchRun` at start, runs each station in order via a
        :class:`StationRunner` (or the Reviewer Station for a reviewer station),
        and finalizes the run record. Halts early on a failed station, a
        spend-guard hard stop, a reviewer block/decision, or cancellation.
        """

        mode = autonomy_mode if isinstance(autonomy_mode, AutonomyMode) else AutonomyMode(autonomy_mode)
        rid = run_id or f"run_{uuid4().hex}"
        intent = route_intent if route_intent is not None else _route_intent(route)

        run = DispatchRun(
            id=rid,
            job_id=manifest.job_id,
            manifest_id=manifest.id,
            route_id=route.id,
            started_at=self._clock(),
            status="running",
        )
        self._ledger.write_run(run)

        return self._walk_stations(
            run=run,
            route=route,
            manifest=manifest,
            mode=mode,
            intent=intent,
            approval_granted=approval_granted,
            start_index=0,
        )

    def resume(
        self,
        *,
        run_id: str,
        route: DispatchRoute,
        manifest: DispatchManifest,
        autonomy_mode: AutonomyMode | str,
        workspace_root: str,
        route_intent: Optional[str] = None,
        approval_granted: bool = False,
    ) -> FulfillmentResult:
        """Resume an interrupted run.

        Reconciles the last station via :func:`reconcile_run`, persists the
        station-status transition, and — depending on the reconciliation verdict —
        continues with the next station, reruns the interrupted station, or
        surfaces a :class:`DispatchDecision` (drift / unknown state). Multi-effect
        auto-rollback is never performed.
        """

        mode = autonomy_mode if isinstance(autonomy_mode, AutonomyMode) else AutonomyMode(autonomy_mode)
        intent = route_intent if route_intent is not None else _route_intent(route)

        run = self._ledger.read_run(run_id)
        if run is None:
            raise KeyError(f"cannot resume unknown run {run_id!r}")

        station_runs = self._ledger.read_station_runs_for_run(run_id)
        effects_for_last = (
            self._ledger.read_effects_for_station_run(station_runs[-1].id)
            if station_runs
            else []
        )
        outcome = reconcile_run(
            station_runs=station_runs,
            effects_for_last_station=effects_for_last,
            workspace_root=workspace_root,
            now=self._clock(),
        )

        # Persist any station-status transition the reconciliation directs.
        if station_runs and outcome.station_status_transition is not None:
            self._transition_station(station_runs[-1], outcome.station_status_transition)

        # A decision halts the resume — record it and return.
        if outcome.action is ResumeAction.DECISION_REQUIRED and outcome.decision is not None:
            self._ledger.write_decision(outcome.decision)
            run = self._finalize_run(run, status="blocked", decision=outcome.decision)
            return FulfillmentResult(
                status=LineStatus.DECISION_REQUIRED,
                run=run,
                station_runs=station_runs,
                decision=outcome.decision,
                reason=outcome.reason,
            )

        # Promote planned effects that the reconciliation found were applied.
        for effect_id in outcome.promote_effect_ids:
            self._ledger.mark_effect_applied(effect_id)

        # Determine where to continue from.
        start_index = self._resume_start_index(route, station_runs, outcome)
        return self._walk_stations(
            run=run,
            route=route,
            manifest=manifest,
            mode=mode,
            intent=intent,
            approval_granted=approval_granted,
            start_index=start_index,
            prior_station_runs=station_runs,
        )

    # -- the sequential walk -------------------------------------------------

    def _walk_stations(
        self,
        *,
        run: DispatchRun,
        route: DispatchRoute,
        manifest: DispatchManifest,
        mode: AutonomyMode,
        intent: Optional[str],
        approval_granted: bool,
        start_index: int,
        prior_station_runs: Optional[list[DispatchStationRun]] = None,
    ) -> FulfillmentResult:
        """Walk ``route.stations`` sequentially from ``start_index`` (no parallel)."""

        station_runs: list[DispatchStationRun] = list(prior_station_runs or [])
        late_results: list[LateResult] = []
        effect_ids: list[str] = []
        reviewer_result: Optional[ReviewerStationResult] = None
        last_build_station_run: Optional[DispatchStationRun] = next(
            (sr for sr in reversed(station_runs) if sr.station_id != "review"),
            None,
        )

        stations = route.stations
        for index in range(start_index, len(stations)):
            station = stations[index]

            # Cancellation: mark this + all remaining queued stations cancelled.
            if self._cancel.is_cancelled():
                self._mark_remaining_cancelled(run, stations[index:])
                run = self._finalize_run(run, status="cancelled")
                return FulfillmentResult(
                    status=LineStatus.CANCELLED,
                    run=run,
                    station_runs=station_runs,
                    late_results=late_results,
                    effect_ids=effect_ids,
                    reviewer_result=reviewer_result,
                    reason="Cancellation requested; remaining stations marked cancelled.",
                )

            # Reviewer station vs worker station.
            if _is_reviewer_station(station):
                reviewer_result, review_run, reviewer_input = self._run_reviewer_station(
                    run=run,
                    route=route,
                    manifest=manifest,
                    station=station,
                    build_station_run=last_build_station_run,
                    effect_ids=effect_ids,
                )
                if review_run is not None:
                    station_runs.append(review_run)
                # Reviewer ran → evaluate the Delivery Gate now.
                package = self._gatehouse.evaluate_delivery(
                    job_id=manifest.job_id,
                    manifest=manifest,
                    route=route,
                    reviewer_result=reviewer_result,
                    station_runs=station_runs,
                    delivery_package_fields=_delivery_fields(
                        route,
                        station_runs,
                        reviewer_result=reviewer_result,
                        reviewer_input=reviewer_input,
                    ),
                    route_uses_reviewer=True,
                )
                return self._finalize_with_delivery(
                    run=run,
                    package=package,
                    station_runs=station_runs,
                    late_results=late_results,
                    effect_ids=effect_ids,
                    reviewer_result=reviewer_result,
                )

            # Worker station: run via a StationRunner.
            outcome = self._run_worker_station(
                run=run,
                manifest=manifest,
                station=station,
                mode=mode,
                intent=intent,
                approval_granted=approval_granted,
            )
            station_runs.append(outcome.station_run)
            effect_ids.extend(outcome.effect_ids)
            if outcome.late_result is not None:
                late_results.append(outcome.late_result)
            last_build_station_run = outcome.station_run

            # Cancellation surfaced mid-station (late result captured).
            if outcome.station_run.status is StationRunStatus.CANCELLED:
                self._mark_remaining_cancelled(run, stations[index + 1:])
                run = self._finalize_run(run, status="cancelled")
                return FulfillmentResult(
                    status=LineStatus.CANCELLED,
                    run=run,
                    station_runs=station_runs,
                    late_results=late_results,
                    effect_ids=effect_ids,
                    reason="Cancellation propagated mid-station; late result captured.",
                )

            # Spend Guard hard stop: surface a decision, halt the line.
            if outcome.failure_reason == SPEND_GUARD_EXCEEDED_REASON:
                decision = self._build_spend_guard_decision(run, station)
                self._ledger.write_decision(decision)
                run = self._finalize_run(run, status="blocked", decision=decision)
                return FulfillmentResult(
                    status=LineStatus.DECISION_REQUIRED,
                    run=run,
                    station_runs=station_runs,
                    decision=decision,
                    late_results=late_results,
                    effect_ids=effect_ids,
                    reason="Spend Guard hard-stopped a station.",
                )

            # Any other station failure halts the line (no automatic repair loop).
            if outcome.station_run.status is StationRunStatus.FAILED:
                run = self._finalize_run(run, status="failed")
                return FulfillmentResult(
                    status=LineStatus.FAILED,
                    run=run,
                    station_runs=station_runs,
                    late_results=late_results,
                    effect_ids=effect_ids,
                    reason=f"Station {station.id!r} failed; line halted.",
                )

        # Walked every station with no reviewer gate (e.g. quick_answer): the
        # line is delivered. Build a delivery package via the Gatehouse with no
        # reviewer (a pass-through structural gate).
        package = self._gatehouse.evaluate_delivery(
            job_id=manifest.job_id,
            manifest=manifest,
            route=route,
            reviewer_result=ReviewerStationResult.for_status("pass"),
            station_runs=station_runs,
            delivery_package_fields=_delivery_fields(
                route,
                station_runs,
                reviewer_result=ReviewerStationResult.for_status("pass"),
            ),
            route_uses_reviewer=False,
        )
        return self._finalize_with_delivery(
            run=run,
            package=package,
            station_runs=station_runs,
            late_results=late_results,
            effect_ids=effect_ids,
            reviewer_result=reviewer_result,
        )

    # -- worker station ------------------------------------------------------

    def _run_worker_station(
        self,
        *,
        run: DispatchRun,
        manifest: DispatchManifest,
        station: RouteStation,
        mode: AutonomyMode,
        intent: Optional[str],
        approval_granted: bool,
    ):
        """Resolve the worker + overlay, then run the station via a StationRunner."""

        worker = self._resolve_worker(station)
        overlay = self._resolve_overlay(station)
        # Capability intersection: the worker must satisfy the overlay's and
        # the station's required capabilities or the binding fails loud. The
        # route was already bound by select_route, but re-asserting here keeps the
        # station runner's contract local and explicit.
        assert_route_binding(worker, overlay, station.required_capabilities)

        runner = StationRunner(
            worker_llm=self._worker_llm,
            context_provider=self._context_provider,
            ledger=self._ledger,
            spend_guard=self._spend_guard,
            cancel_token=self._cancel,
            tool_runner=self._tool_runner,
            clock=self._clock,
        )
        outcome = runner.run(
            run_id=run.id,
            manifest=manifest,
            station=station,
            worker=worker,
            autonomy_mode=mode,
            overlay=overlay,
            route_intent=intent,
            approval_granted=approval_granted,
        )
        # Append the station run id onto the run record (kept current as we go).
        self._append_station_run(run, outcome.station_run.id)
        for effect_id in outcome.effect_ids:
            self._append_effect(run, effect_id)
        if outcome.late_result is not None:
            self._append_late_result(run, outcome.late_result.id)
        return outcome

    def _resolve_worker(self, station: RouteStation) -> DispatchWorker:
        """Resolve the single worker that staffs a worker station (deterministic).

        Picks the first registry worker (by id) that declares the station's role
        and possesses every required capability — the same capability-intersection
        rule as ``resolve_station_workers``, reduced to a single deterministic
        pick for sequential execution.
        """

        role = station.required_role
        required = set(station.required_capabilities)
        for worker in self._workers.for_role(role):
            if required.issubset(set(worker.capabilities)):
                return worker
        raise RuntimeError(
            f"no worker staffs station {station.id!r} (role {role!r}, "
            f"capabilities {sorted(required)!r}); route should have been rejected "
            "at selection time"
        )

    def _resolve_overlay(self, station: RouteStation) -> Optional[PromptOverlay]:
        """Load the station's prompt overlay, or ``None`` when it declares none."""

        if not station.prompt_overlay:
            return None
        return self._overlay_loader.load(station.prompt_overlay)

    # -- reviewer station ----------------------------------------------------

    def _run_reviewer_station(
        self,
        *,
        run: DispatchRun,
        route: DispatchRoute,
        manifest: DispatchManifest,
        station: RouteStation,
        build_station_run: Optional[DispatchStationRun],
        effect_ids: list[str],
    ) -> tuple[ReviewerStationResult, Optional[DispatchStationRun], ReviewerStationInput]:
        """Run the Reviewer Station and commit its station-run record.

        Requires a reviewer client to have been injected; raises if absent (a
        route with a reviewer station cannot run without one). Builds the
        :class:`ReviewerStationInput` from the manifest + the upstream build
        station's output, makes exactly one review call, and commits a
        ``completed`` :class:`DispatchStationRun` for the reviewer station.
        """

        if self._reviewer_llm is None:
            raise RuntimeError(
                f"route {route.id!r} has a reviewer station {station.id!r} but no "
                "reviewer client was injected into the FulfillmentLine"
            )

        reviewer = ReviewerStation(self._reviewer_llm)
        effects = [
            effect
            for effect_id in effect_ids
            if (effect := self._ledger.read_effect(effect_id)) is not None
        ]
        artifacts = (
            self._ledger.read_artifacts_for_station_run(build_station_run.id)
            if build_station_run is not None
            else []
        )
        review_input = build_reviewer_input(
            manifest,
            route,
            build_station_run,
            self._ledger,
            effects=effects,
            artifacts=artifacts,
            delivery_fields=_delivery_fields(route, [r for r in [build_station_run] if r]),
        )
        result = reviewer.review(review_input)

        worker = self._resolve_worker(station)
        review_run = DispatchStationRun(
            id=f"stationrun_{uuid4().hex}",
            run_id=run.id,
            station_id=station.id,
            worker_id=worker.id,
            prompt_overlay_id=None,
            context_bundle_id=f"reviewer_{manifest.id}_{station.id}",
            tip_request_ids=[f"tip_{run.id}_review"],
            status=StationRunStatus.COMPLETED,
            iteration_count=1,
            tool_call_count=0,
            wall_seconds=0,
            result_payload=result.model_dump(mode="json"),
            result_schema_version="reviewer_station_result.v1",
            attempt_number=1,
        )
        # Commit only after the schema-valid reviewer output exists (criterion 4).
        self._ledger.write_station_run(review_run)
        self._append_station_run(run, review_run.id)
        return result, review_run, review_input

    # -- delivery + finalization --------------------------------------------

    def _finalize_with_delivery(
        self,
        *,
        run: DispatchRun,
        package: DeliveryPackage,
        station_runs: list[DispatchStationRun],
        late_results: list[LateResult],
        effect_ids: list[str],
        reviewer_result: Optional[ReviewerStationResult],
    ) -> FulfillmentResult:
        """Map a Gatehouse :class:`DeliveryPackage` onto the line result + run status."""

        if package.decision is not None:
            self._ledger.write_decision(package.decision)
            self._append_decision(run, package.decision.id)

        status_map = {
            DeliveryStatus.DELIVERY_READY: (LineStatus.DELIVERED, "delivered"),
            DeliveryStatus.DELIVERY_READY_WITH_WARNING: (
                LineStatus.DELIVERY_READY_WITH_WARNING,
                "delivery_ready",
            ),
            DeliveryStatus.DECISION_REQUIRED: (LineStatus.DECISION_REQUIRED, "gate_review"),
            DeliveryStatus.BLOCKED: (LineStatus.BLOCKED, "blocked"),
        }
        line_status, run_status = status_map[package.status]
        run = self._finalize_run(run, status=run_status, decision=package.decision)

        receipt = None
        if line_status in (LineStatus.DELIVERED, LineStatus.DELIVERY_READY_WITH_WARNING):
            receipt = build_and_write_receipt(
                run=run,
                ledger=self._ledger,
                final_status=run_status,
                clock=self._clock,
            )

        return FulfillmentResult(
            status=line_status,
            run=run,
            station_runs=station_runs,
            decision=package.decision,
            delivery_package=package,
            late_results=late_results,
            effect_ids=effect_ids,
            reviewer_result=reviewer_result,
            receipt=receipt,
            reason=package.summary,
        )

    def _finalize_run(
        self,
        run: DispatchRun,
        *,
        status: str,
        decision: Optional[DispatchDecision] = None,
    ) -> DispatchRun:
        """Set the run's terminal status + ended_at and persist it atomically.

        When a ``decision`` halted the run it is linked onto ``run.decisions`` (if
        not already there) so the Run Ledger record references it — the decision
        itself is written by the caller.
        """

        decisions = list(run.decisions)
        if decision is not None and decision.id not in decisions:
            decisions.append(decision.id)
        run = run.model_copy(
            update={"status": status, "ended_at": self._clock(), "decisions": decisions}
        )
        self._ledger.write_run(run)
        return run

    # -- resume helpers ------------------------------------------------------

    def _resume_start_index(
        self,
        route: DispatchRoute,
        station_runs: list[DispatchStationRun],
        outcome: ResumeOutcome,
    ) -> int:
        """Pick the station index to resume from given the reconciliation verdict.

        * CONTINUE_NEXT_STATION → the station after the last completed one.
        * PROMOTE_AND_CONTINUE → the station after the interrupted one.
        * RERUN_STATION → the interrupted station itself.
        """

        if not station_runs:
            return 0
        last = station_runs[-1]
        last_index = _station_index(route, last.station_id)
        if outcome.action in (ResumeAction.CONTINUE_NEXT_STATION, ResumeAction.PROMOTE_AND_CONTINUE):
            return last_index + 1
        # RERUN_STATION → rerun the interrupted station.
        return last_index

    def _transition_station(
        self, station_run: DispatchStationRun, status: StationRunStatus
    ) -> None:
        """Persist a station-run status transition (resume reconciliation)."""

        updated = station_run.model_copy(update={"status": status})
        self._ledger.write_station_run(updated)

    # -- run-record append helpers (keep the DispatchRun lists current) ------

    def _append_station_run(self, run: DispatchRun, station_run_id: str) -> None:
        if station_run_id not in run.station_runs:
            run.station_runs.append(station_run_id)
            self._ledger.write_run(run)

    def _append_decision(self, run: DispatchRun, decision_id: str) -> None:
        if decision_id not in run.decisions:
            run.decisions.append(decision_id)
            self._ledger.write_run(run)

    def _append_effect(self, run: DispatchRun, effect_id: str) -> None:
        if effect_id not in run.effects:
            run.effects.append(effect_id)
            self._ledger.write_run(run)

    def _append_late_result(self, run: DispatchRun, late_result_id: str) -> None:
        if late_result_id not in run.late_results:
            run.late_results.append(late_result_id)
            self._ledger.write_run(run)

    # -- cancellation --------------------------------------------------------

    def _mark_remaining_cancelled(
        self, run: DispatchRun, remaining: list[RouteStation]
    ) -> None:
        """Mark every not-yet-run station ``cancelled``.

        Each queued station gets a ``cancelled`` :class:`DispatchStationRun` so
        the Run Ledger records exactly which stations never ran.
        """

        for station in remaining:
            worker_id = self._cancelled_worker_id(station)
            cancelled = DispatchStationRun(
                id=f"stationrun_{uuid4().hex}",
                run_id=run.id,
                station_id=station.id,
                worker_id=worker_id,
                prompt_overlay_id=station.prompt_overlay,
                context_bundle_id="(not-built: cancelled)",
                tip_request_ids=[],
                status=StationRunStatus.CANCELLED,
                iteration_count=0,
                tool_call_count=0,
                wall_seconds=0,
                result_payload=None,
                result_schema_version=station.output_schema,
                attempt_number=1,
            )
            self._ledger.write_station_run(cancelled)
            self._append_station_run(run, cancelled.id)

    def _cancelled_worker_id(self, station: RouteStation) -> str:
        """Best-effort worker id for a cancelled station's record (never raises)."""

        if not is_worker_station(station):
            return station.system_component or "system_component"
        try:
            return self._resolve_worker(station).id
        except RuntimeError:
            return f"role:{station.required_role}"

    # -- decision builders ---------------------------------------------------

    def _build_spend_guard_decision(
        self, run: DispatchRun, station: RouteStation
    ) -> DispatchDecision:
        """Build the Spend-Guard decision (raise budget / change route / cancel)."""

        return DispatchDecision(
            id=f"decision_{run.id}_spend_guard",
            job_id=run.job_id,
            created_at=self._clock(),
            scope=DecisionScope.STATION,
            title="Spend Guard hard-stopped a station",
            question=(
                f"The Spend Guard cap was reached while running station "
                f"{station.id!r}. Raise the budget, change the route, or cancel "
                "the job?"
            ),
            reason=(
                "A station hit the Spend Guard cap hard "
                "stop (reason=spend_guard_exceeded). Dispatch surfaces a decision "
                "rather than bypassing Spend Guard."
            ),
            risk_level=RiskLevel.MEDIUM,
            options=[
                DecisionOption(
                    id="raise_budget",
                    label="Raise the budget",
                    description="Increase the Spend Guard cap and continue the station.",
                    tradeoffs=["Spends more tokens than the original cap allowed."],
                ),
                DecisionOption(
                    id="change_route",
                    label="Change the route",
                    description="Re-route the job to a cheaper route.",
                    tradeoffs=["May produce a less thorough result."],
                ),
                DecisionOption(
                    id="cancel_job",
                    label="Cancel the job",
                    description="Stop the job; perform no further work.",
                    tradeoffs=["No further work is performed."],
                ),
            ],
            recommendation=DecisionRecommendation(
                option_id="raise_budget",
                rationale="Raising the budget resumes the in-flight work with least disruption.",
            ),
            default_action=DecisionDefaultAction(
                option_id="raise_budget", auto_apply_after=AutoApplyAfter.NEVER
            ),
            status=DecisionStatus.PENDING,
        )


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _is_reviewer_station(station: RouteStation) -> bool:
    """A reviewer station is a worker station whose required role is ``reviewer``."""

    return is_worker_station(station) and station.required_role == "reviewer"


def _route_intent(route: DispatchRoute) -> Optional[str]:
    """Derive the route's intent for loop-policy wall-second defaults.

    Prefers the route's first declared trigger intent; falls back to matching the
    route id against the known route wall-second default keys.
    """

    if route.triggers.intents:
        return route.triggers.intents[0]
    for intent in ROUTE_WALL_SECOND_DEFAULTS:
        if intent in route.id:
            return intent
    return None


def _station_index(route: DispatchRoute, station_id: str) -> int:
    """Index of ``station_id`` within ``route.stations`` (−1 if absent)."""

    for index, station in enumerate(route.stations):
        if station.id == station_id:
            return index
    return -1


def build_reviewer_input(
    manifest: DispatchManifest,
    route: DispatchRoute,
    build_station_run: Optional[DispatchStationRun],
    ledger: RunLedger,
    effects: list[DispatchEffect] | None = None,
    artifacts: list[DispatchArtifact] | None = None,
    delivery_fields: dict[str, Any] | None = None,
) -> ReviewerStationInput:
    """Assemble evidence-backed input for a Reviewer Station.

    The boundary is explicit so reviewer evidence does not get spread through
    the station runner. Evidence comes from the build station payload, Run
    Ledger effect/artifact records, and the delivery-field projection.
    """

    payload = dict(build_station_run.result_payload or {}) if build_station_run else {}
    effect_records = list(effects or []) + _payload_effect_records(payload)
    artifact_records = _dedupe_artifacts(list(artifacts or []) + _payload_artifacts(payload))
    test_results = _payload_test_results(payload)
    command_evidence = _dedupe_command_evidence(
        _payload_command_evidence(payload)
        + _command_evidence(effect_records, test_results)
    )
    file_hashes = _file_hashes(effect_records, payload)
    patch = _payload_text(
        payload,
        "proposed_or_applied_patch",
        "patch",
        "diff",
        "applied_patch",
    )
    diff_summary = str(payload.get("diff_summary") or _diff_summary(effect_records, patch))
    known_risk_flags = _payload_strings(payload, "risk_flags", "known_risk_flags")
    if not known_risk_flags:
        known_risk_flags = [f"default_risk:{route.default_risk.value}"]
    negative_evidence = _payload_negative_evidence(payload)
    if _requires_code_evidence(route):
        negative_evidence.extend(
            _missing_code_evidence(
                patch=patch,
                effect_records=effect_records,
                artifacts=artifact_records,
                test_results=test_results,
                command_evidence=command_evidence,
                file_hashes=file_hashes,
                diff_summary=diff_summary,
            )
        )

    context_summary = str(payload.get("context_summary") or manifest.goal)
    if delivery_fields and delivery_fields.get("summary"):
        context_summary = f"{context_summary}\nDelivery summary: {delivery_fields['summary']}"

    return ReviewerStationInput(
        manifest_id=manifest.id,
        route_id=route.id,
        build_station_result_id=(
            build_station_run.id if build_station_run is not None else "stationrun_none"
        ),
        acceptance_criteria=list(manifest.acceptance_criteria),
        constraints=list(manifest.constraints),
        proposed_or_applied_patch=patch,
        effect_records=effect_records,
        artifacts=artifact_records,
        context_summary=context_summary,
        known_risk_flags=known_risk_flags,
        test_results=test_results,
        command_evidence=command_evidence,
        file_hashes=file_hashes,
        diff_summary=diff_summary,
        negative_evidence=negative_evidence,
    )


def _delivery_fields(
    route: DispatchRoute,
    station_runs: list[DispatchStationRun],
    *,
    reviewer_result: Optional[ReviewerStationResult] = None,
    reviewer_input: Optional[ReviewerStationInput] = None,
) -> dict[str, Any]:
    """Assemble evidence-backed delivery fields for Gatehouse checks."""

    fields: dict[str, Any] = {}
    delivery = route.delivery
    if delivery.include_summary:
        fields["summary"] = _delivery_summary(route, station_runs, reviewer_result)
    if delivery.include_files_changed:
        fields["files_changed"] = _files_changed(station_runs, reviewer_input)
    if delivery.include_tests:
        fields["tests"] = _test_delivery_entries(reviewer_input)
    if delivery.include_risks:
        fields["risks"] = _risk_entries(reviewer_result, reviewer_input)
    if delivery.include_next_steps:
        fields["next_steps"] = _next_steps(reviewer_result)
    if reviewer_input is not None and reviewer_input.negative_evidence:
        fields["negative_evidence"] = [
            item.model_dump(mode="json") for item in reviewer_input.negative_evidence
        ]
    return fields


def _delivery_summary(
    route: DispatchRoute,
    station_runs: list[DispatchStationRun],
    reviewer_result: Optional[ReviewerStationResult],
) -> str:
    completed = sum(1 for run in station_runs if run.status is StationRunStatus.COMPLETED)
    reviewer = (
        f"; reviewer_status={reviewer_result.status.value}"
        if reviewer_result is not None
        else ""
    )
    return f"Route {route.id} ran {len(station_runs)} station(s), {completed} completed{reviewer}."


def _files_changed(
    station_runs: list[DispatchStationRun],
    reviewer_input: Optional[ReviewerStationInput],
) -> list[str]:
    changed: list[str] = []
    if reviewer_input is not None:
        changed.extend(
            effect.target
            for effect in reviewer_input.effect_records
            if effect.target_type.value == "file"
        )
        changed.extend(
            _payload_strings(dict(run.result_payload or {}), "files_changed")
            for run in station_runs
        )
    else:
        changed.extend(
            _payload_strings(dict(run.result_payload or {}), "files_changed")
            for run in station_runs
        )
    flat = _flatten_strings(changed)
    if flat:
        return sorted(set(flat))
    if reviewer_input is not None and reviewer_input.negative_evidence:
        return [
            f"negative_evidence:{item.field}"
            for item in reviewer_input.negative_evidence
            if item.field in {"effect_records", "file_hashes", "diff_summary", "artifacts"}
        ] or ["negative_evidence:files_changed"]
    return []


def _test_delivery_entries(
    reviewer_input: Optional[ReviewerStationInput],
) -> list[dict[str, Any]]:
    if reviewer_input is None:
        return []
    if reviewer_input.test_results:
        return [test.model_dump(mode="json") for test in reviewer_input.test_results]
    return [
        item.model_dump(mode="json")
        for item in reviewer_input.negative_evidence
        if item.field in {"test_results", "test_exit_codes", "test_logs"}
    ]


def _risk_entries(
    reviewer_result: Optional[ReviewerStationResult],
    reviewer_input: Optional[ReviewerStationInput],
) -> list[str]:
    risks: list[str] = []
    if reviewer_input is not None:
        risks.extend(reviewer_input.known_risk_flags)
        risks.extend(
            f"negative_evidence:{item.field}"
            for item in reviewer_input.negative_evidence
            if item.blocks_required_acceptance_criteria
        )
    if reviewer_result is not None:
        risks.extend(
            f"{flag.id}:{flag.severity.value}" for flag in reviewer_result.risk_flags
        )
    return risks or ["none identified from reviewer and deterministic checks"]


def _next_steps(reviewer_result: Optional[ReviewerStationResult]) -> list[str]:
    if reviewer_result is None:
        return ["delivery gate evaluated structural evidence"]
    status = reviewer_result.status.value
    if status == "pass":
        return ["deliver the package"]
    if status == "warning":
        return ["resolve the reviewer warning decision"]
    if status == "not_evaluable":
        return ["provide missing required evidence before delivery"]
    return ["address required fixes before delivery"]


def _requires_code_evidence(route: DispatchRoute) -> bool:
    return route.id == "route.code_task.v1"


def _missing_code_evidence(
    *,
    patch: str | None,
    effect_records: list[DispatchEffect],
    artifacts: list[DispatchArtifact],
    test_results: list[EvidenceTestResult],
    command_evidence: list[CommandEvidence],
    file_hashes: dict[str, str],
    diff_summary: str,
) -> list[NegativeEvidence]:
    missing: list[NegativeEvidence] = []
    if not patch:
        missing.append(_negative("proposed_or_applied_patch", "no patch evidence supplied"))
    if not effect_records:
        missing.append(_negative("effect_records", "no DispatchEffect records supplied"))
    if not artifacts:
        missing.append(_negative("artifacts", "no DispatchArtifact references supplied"))
    if not test_results:
        missing.append(_negative("test_results", "no test command results supplied"))
    if not command_evidence:
        missing.append(_negative("command_evidence", "no command evidence supplied"))
    if not file_hashes:
        missing.append(_negative("file_hashes", "no before/after file hashes supplied"))
    if not diff_summary:
        missing.append(_negative("diff_summary", "no diff summary supplied"))
    if test_results and any(test.exit_code is None for test in test_results):
        missing.append(_negative("test_exit_codes", "one or more test results lack exit codes"))
    if test_results and not any(test.stdout_excerpt or test.stderr_excerpt for test in test_results):
        missing.append(_negative("test_logs", "test results lack bounded log excerpts"))
    return missing


def _negative(field: str, reason: str) -> NegativeEvidence:
    return NegativeEvidence(
        field=field,
        status="missing",
        reason=reason,
        consequence="cannot verify required code_task acceptance criteria",
    )


def _payload_text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _payload_strings(payload: dict[str, Any], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        raw = payload.get(key)
        if raw is None:
            continue
        if isinstance(raw, str):
            values.append(raw)
        elif isinstance(raw, list):
            values.extend(str(item) for item in raw if str(item))
    return values


def _flatten_strings(values: list[Any]) -> list[str]:
    flat: list[str] = []
    for value in values:
        if isinstance(value, list):
            flat.extend(str(item) for item in value if str(item))
        elif value:
            flat.append(str(value))
    return flat


def _payload_test_results(payload: dict[str, Any]) -> list[EvidenceTestResult]:
    raw = payload.get("test_results", payload.get("tests", []))
    if isinstance(raw, dict):
        items = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    results: list[EvidenceTestResult] = []
    for item in items:
        if isinstance(item, EvidenceTestResult):
            results.append(item)
        elif isinstance(item, dict):
            exit_code = item.get("exit_code")
            status = str(item.get("status") or _status_from_exit_code(exit_code))
            results.append(
                EvidenceTestResult(
                    command=str(item.get("command") or item.get("name") or "unknown"),
                    status=status,
                    exit_code=exit_code if isinstance(exit_code, int) else None,
                    stdout_excerpt=str(
                        item.get("stdout_excerpt") or item.get("log_excerpt") or ""
                    ),
                    stderr_excerpt=str(item.get("stderr_excerpt") or ""),
                )
            )
        elif isinstance(item, str):
            results.append(EvidenceTestResult(command=item, status="declared"))
    return results


def _payload_command_evidence(payload: dict[str, Any]) -> list[CommandEvidence]:
    raw = payload.get("command_evidence", payload.get("declared_test_commands", []))
    if isinstance(raw, dict):
        items = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    records: list[CommandEvidence] = []
    for item in items:
        if isinstance(item, CommandEvidence):
            records.append(item)
        elif isinstance(item, dict):
            exit_code = item.get("exit_code")
            records.append(
                CommandEvidence(
                    command=str(item.get("command") or item.get("name") or "unknown"),
                    status=str(item.get("status") or "declared"),
                    exit_code=exit_code if isinstance(exit_code, int) else None,
                    log_excerpt=str(item.get("log_excerpt") or ""),
                    effect_id=(
                        str(item["effect_id"])
                        if item.get("effect_id") is not None
                        else None
                    ),
                )
            )
        elif isinstance(item, str):
            records.append(CommandEvidence(command=item, status="declared"))
    return records


def _payload_effect_records(payload: dict[str, Any]) -> list[DispatchEffect]:
    raw = payload.get("effect_records", [])
    if not isinstance(raw, list):
        return []
    records: list[DispatchEffect] = []
    for item in raw:
        if isinstance(item, DispatchEffect):
            records.append(item)
        elif isinstance(item, dict):
            records.append(DispatchEffect.model_validate(item))
    return records


def _payload_artifacts(payload: dict[str, Any]) -> list[DispatchArtifact]:
    raw = payload.get("artifacts", [])
    if not isinstance(raw, list):
        return []
    records: list[DispatchArtifact] = []
    for item in raw:
        if isinstance(item, DispatchArtifact):
            records.append(item)
        elif isinstance(item, dict):
            records.append(DispatchArtifact.model_validate(item))
    return records


def _payload_negative_evidence(payload: dict[str, Any]) -> list[NegativeEvidence]:
    raw = payload.get("negative_evidence", [])
    if not isinstance(raw, list):
        return []
    records: list[NegativeEvidence] = []
    for item in raw:
        if isinstance(item, NegativeEvidence):
            records.append(item)
        elif isinstance(item, dict):
            records.append(NegativeEvidence.model_validate(item))
    return records


def _dedupe_artifacts(artifacts: list[DispatchArtifact]) -> list[DispatchArtifact]:
    seen: set[str] = set()
    deduped: list[DispatchArtifact] = []
    for artifact in artifacts:
        if artifact.id in seen:
            continue
        seen.add(artifact.id)
        deduped.append(artifact)
    return deduped


def _dedupe_command_evidence(evidence: list[CommandEvidence]) -> list[CommandEvidence]:
    seen: set[tuple[str, str | None]] = set()
    deduped: list[CommandEvidence] = []
    for item in evidence:
        key = (item.command, item.effect_id)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _command_evidence(
    effects: list[DispatchEffect],
    tests: list[EvidenceTestResult],
) -> list[CommandEvidence]:
    evidence = [
        CommandEvidence(
            command=test.command,
            status=test.status,
            exit_code=test.exit_code,
            log_excerpt=test.stdout_excerpt or test.stderr_excerpt,
        )
        for test in tests
    ]
    evidence.extend(
        CommandEvidence(
            command=effect.target,
            status=effect.status.value,
            effect_id=effect.id,
        )
        for effect in effects
        if effect.target_type.value == "command_output"
    )
    return evidence


def _file_hashes(
    effects: list[DispatchEffect],
    payload: dict[str, Any],
) -> dict[str, str]:
    raw = payload.get("file_hashes")
    hashes: dict[str, str] = (
        {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    )
    for effect in effects:
        if effect.target_type.value != "file":
            continue
        digest = effect.after_hash or effect.before_hash
        if digest:
            hashes[effect.target] = digest
    return hashes


def _status_from_exit_code(exit_code: Any) -> str:
    if exit_code == 0:
        return "passed"
    if exit_code is not None:
        return "failed"
    return "unknown"


def _diff_summary(effects: list[DispatchEffect], patch: str | None) -> str:
    file_targets = [
        effect.target for effect in effects if effect.target_type.value == "file"
    ]
    command_targets = [
        effect.target for effect in effects if effect.target_type.value == "command_output"
    ]
    pieces: list[str] = []
    if file_targets:
        pieces.append(f"file effects: {', '.join(sorted(file_targets))}")
    if command_targets:
        pieces.append(f"command effects: {', '.join(sorted(command_targets))}")
    if patch and not file_targets:
        pieces.append("patch text supplied without file effect records")
    return "; ".join(pieces)


__all__ = [
    "LineStatus",
    "FulfillmentResult",
    "FulfillmentLine",
    "build_reviewer_input",
]
