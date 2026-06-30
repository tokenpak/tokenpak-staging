"""Deliberation Engine — minimum path (deliberation policy §11, Phase 1 minimum increment).

Just enough Engine to emit a conformant Deliberation Receipt for a given
decision input: the caller supplies pre-collected normalized node outputs
(§6) — typically gathered through provider adapters by an upstream surface —
and the Engine runs disagreement detection (§7), preserves dissent, produces
a rule-based synthesis, and writes a file-based JSON Receipt (§8) under the
TokenPak home state root. **No model, network, or provider call is made
anywhere in this module**; live multi-provider routing is a later Phase 1
increment behind the provider-adapter seam.

Bounded-deliberation guarantees implemented here (§5.1):

- **Anti-recursion** — a deliberation run may not spawn a nested run,
  directly or indirectly (:class:`DeliberationRecursionError`).
- **Deterministic fallback** — when the scorer escalates and no judge result
  is available, the receipt carries the majority position with
  ``fallback_reason`` set instead of looping or silently dropping.
- **Receipt on all exit paths** — success, error abort, and explicit
  policy/budget/user stops all emit a receipt. The error path writes a
  ``partial_error_abort`` receipt and re-raises.

Mode is ``advisory`` by default — shadow-before-gate (§15.2) is the standing
constraint; promoting any risk class to ``gating`` is an operator decision
made after reviewing the shadow agreement record, never a code default.
"""

from __future__ import annotations

import uuid
from collections import Counter
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from pydantic import Field

from tokenpak import _paths

from .models import (
    VERDICT_ORDER,
    DeliberationBaseModel,
    DeliberationReceipt,
    DisagreementResult,
    DissentRecord,
    Mode,
    NodeOutput,
    PartialResult,
    ResultState,
    Verdict,
)
from .scorer import ScorerThresholds, score

ENGINE_VERSION = "0.1.0-minimum"

_in_deliberation: ContextVar[bool] = ContextVar("tokenpak_in_deliberation", default=False)


class DeliberationRecursionError(RuntimeError):
    """Raised when a deliberation run would nest inside another (§5.1)."""


class DeliberationConfig(DeliberationBaseModel):
    """Engine configuration. Ceilings exist and are configurable (§5.1)."""

    mode: Mode = "advisory"
    max_challenger_rounds: int = Field(
        default=0, ge=0, description="Minimum path consumes pre-collected outputs (0 rounds)"
    )
    max_judge_passes: int = Field(default=1, ge=0)
    thresholds: ScorerThresholds = Field(default_factory=ScorerThresholds)
    receipts_dir: Path | None = Field(
        default=None,
        description="Override for tests/embedding; defaults to the home-layout path "
        "_paths.under('deliberation', 'receipts')",
    )


class DeliberationInput(DeliberationBaseModel):
    """One decision to deliberate, with its pre-collected node outputs."""

    decision_ref: str
    risk_class: str
    nodes: list[NodeOutput]
    correlation_id: str | None = None
    context_package_ref: str | None = None


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _majority_verdict(nodes: Sequence[NodeOutput]) -> Verdict:
    """Most common verdict; ties break toward the more severe rung (deterministic)."""
    counts = Counter(n.verdict for n in nodes)
    top = max(counts.values())
    return max(
        (v for v, c in counts.items() if c == top),
        key=lambda v: VERDICT_ORDER[v],
    )


class DeliberationEngine:
    """Minimum Deliberation Engine: detect → preserve dissent → synthesize → receipt."""

    def __init__(self, config: DeliberationConfig | None = None) -> None:
        self.config = config or DeliberationConfig()

    # -- receipt store -------------------------------------------------------

    def receipts_dir(self) -> Path:
        if self.config.receipts_dir is not None:
            return self.config.receipts_dir
        return _paths.under("deliberation", "receipts")

    def _write(self, receipt: DeliberationReceipt) -> Path:
        directory = self.receipts_dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / f"{receipt.receipt_id}.json"
        path.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return path

    # -- exit paths ----------------------------------------------------------

    def run(
        self,
        inputs: DeliberationInput,
        scorer: Callable[..., DisagreementResult] = score,
    ) -> DeliberationReceipt:
        """Deliberate one decision and emit a receipt (receipt on ALL exit paths)."""
        if _in_deliberation.get():
            raise DeliberationRecursionError(
                "deliberation must not invoke deliberation (policy §5.1 anti-recursion)"
            )
        token = _in_deliberation.set(True)
        correlation_id = inputs.correlation_id or f"dlb_{uuid.uuid4().hex}"
        try:
            receipt = self._deliberate(inputs, correlation_id, scorer)
        except DeliberationRecursionError:
            raise
        except Exception as exc:
            self._write(self._stub_receipt(
                inputs,
                correlation_id,
                result_state="partial_error_abort",
                partial=PartialResult(
                    completed_nodes=[n.node_id for n in inputs.nodes],
                    usable_intermediate_outputs=[],
                    recommended_resume_mode="rerun",
                    spend_guard_reason=None,
                ),
                synthesis=f"error abort: {type(exc).__name__}",
            ))
            raise
        finally:
            _in_deliberation.reset(token)
        self._write(receipt)
        return receipt

    def emit_stop_receipt(
        self,
        inputs: DeliberationInput,
        result_state: ResultState,
        reason: str,
        completed_nodes: Sequence[str] = (),
    ) -> DeliberationReceipt:
        """Emit a receipt for an explicit policy / budget / user stop (§9)."""
        if result_state == "complete":
            raise ValueError("emit_stop_receipt is for non-complete exit paths")
        correlation_id = inputs.correlation_id or f"dlb_{uuid.uuid4().hex}"
        receipt = self._stub_receipt(
            inputs,
            correlation_id,
            result_state=result_state,
            partial=PartialResult(
                completed_nodes=list(completed_nodes),
                missing_nodes=[
                    n.node_id for n in inputs.nodes if n.node_id not in set(completed_nodes)
                ],
                usable_intermediate_outputs=[],
                recommended_resume_mode="rerun",
                spend_guard_reason=reason if result_state == "partial_budget_abort" else None,
            ),
            synthesis=f"stopped: {reason}",
        )
        self._write(receipt)
        return receipt

    # -- core ----------------------------------------------------------------

    def _deliberate(
        self,
        inputs: DeliberationInput,
        correlation_id: str,
        scorer: Callable[..., DisagreementResult],
    ) -> DeliberationReceipt:
        disagreement = scorer(inputs.nodes, self.config.thresholds)
        majority = _majority_verdict(inputs.nodes) if inputs.nodes else "escalate"
        dissent = [
            DissentRecord(
                node_id=n.node_id,
                model_card=n.model_card,
                verdict=n.verdict,
                reason_codes=list(n.reason_codes),
                risk_flags=list(n.risk_flags),
                summary=n.summary,
            )
            for n in inputs.nodes
            if n.verdict != majority
        ]

        fallback_reason: str | None = None
        if disagreement.escalate:
            # Minimum path has no judge call (§7: judge only on escalation; the
            # fixed-rubric judge is a model call and lands with the provider-
            # adapter increment). Deterministic fallback per §5.1.
            recommendation = "escalate"
            fallback_reason = (
                "scorer escalated and no judge is available on the minimum path"
            )
            adjudication = "escalate"
        else:
            recommendation = majority
            adjudication = "annotate" if dissent else None

        synthesis = (
            f"{len(inputs.nodes)} perspective(s); majority verdict '{majority}'; "
            f"disagreement: {disagreement.classification}; "
            f"{len(dissent)} dissenting position(s) preserved"
        )

        return DeliberationReceipt(
            receipt_id=f"dr_{uuid.uuid4().hex}",
            correlation_id=correlation_id,
            decision_ref=inputs.decision_ref,
            risk_class=inputs.risk_class,
            mode=self.config.mode,  # advisory by default (§15.2 shadow-before-gate)
            providers_consulted=sorted({n.model_card for n in inputs.nodes}),
            node_outputs=list(inputs.nodes),
            disagreement=disagreement,
            dissent=dissent,
            synthesis=synthesis,
            recommendation=recommendation,
            fallback_reason=fallback_reason,
            adjudication=adjudication,
            adjudicated_by="policy:std64-scorer-v0" if adjudication else None,
            result_state="complete",
            context_package_ref=inputs.context_package_ref,
            created_at=_utcnow(),
            engine_version=ENGINE_VERSION,
        )

    def _stub_receipt(
        self,
        inputs: DeliberationInput,
        correlation_id: str,
        *,
        result_state: ResultState,
        partial: PartialResult,
        synthesis: str,
    ) -> DeliberationReceipt:
        return DeliberationReceipt(
            receipt_id=f"dr_{uuid.uuid4().hex}",
            correlation_id=correlation_id,
            decision_ref=inputs.decision_ref,
            risk_class=inputs.risk_class,
            mode=self.config.mode,
            providers_consulted=sorted({n.model_card for n in inputs.nodes}),
            node_outputs=list(inputs.nodes),
            synthesis=synthesis,
            result_state=result_state,
            partial=partial,
            context_package_ref=inputs.context_package_ref,
            created_at=_utcnow(),
            engine_version=ENGINE_VERSION,
        )
