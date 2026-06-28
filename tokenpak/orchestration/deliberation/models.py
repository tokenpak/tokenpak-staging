"""Deliberation Dispatch records (deliberation policy §6 / §8 — minimum Engine path).

Normalized node outputs (§6 enum-retrofit shape), the disagreement-scorer
result (§7), and the Deliberation Receipt contract (§8 OSS shape). Numeric
scoring fields (``judge_score``, ``model_win_label``, ``calibration_delta``,
``downstream_success_label``) are Pro-only and live under
``extensions.tokenpak_paid`` exclusively — they are never top-level receipt
fields, and ``extra="forbid"`` makes adding one a fail-loud validation error.

Privacy (§8.4): every free-text field on these records is a *summary* by
contract. Raw reasoning traces are never persisted durably; in-run
intermediate state is ephemeral and is not part of any model here.

``reason_codes`` / ``risk_flags`` reuse the PAKPlan registry enums
(``tokenpak/registry`` ``schemas/tip/pak-reason-codes-v1.schema.json`` /
``pak-risk-flags-v1.schema.json``). Per the recall join-table precedent the
runtime stays enum-permissive (non-empty strings); registry schemas and
validators enforce membership, so new codes stay additive without a runtime
release.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Verdict = Literal["approve", "revise", "escalate", "stop"]
"""Ordinal verdict ladder (§6): approve < revise < escalate < stop."""

VERDICT_ORDER: dict[str, int] = {"approve": 0, "revise": 1, "escalate": 2, "stop": 3}

Confidence = Literal["low", "medium", "high"]

CONFIDENCE_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

Mode = Literal["advisory", "gating"]

ResultState = Literal[
    "complete",
    "partial_budget_abort",
    "partial_error_abort",
    "stopped_by_policy",
    "stopped_by_user",
]

Adjudication = Literal["block", "annotate", "escalate"]

DisagreementClass = Literal["agree", "minor-divergence", "material-disagreement"]


class DeliberationBaseModel(BaseModel):
    """Base for every Deliberation record: strict, unknown keys rejected."""

    model_config = ConfigDict(extra="forbid")


class NodeOutput(DeliberationBaseModel):
    """Normalized output of one deliberation node (§6 enum-retrofit shape)."""

    node_id: str
    model_card: str = Field(description="Declared model card for the provider/model consulted")
    verdict: Verdict
    reason_codes: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    confidence: Confidence
    summary: str = Field(default="", description="Free-text summary — never raw reasoning")

    @model_validator(mode="after")
    def _reason_codes_required_unless_approve(self) -> "NodeOutput":
        if not self.reason_codes and self.verdict != "approve":
            raise ValueError(
                "reason_codes may be empty only with verdict 'approve' (policy §6)"
            )
        return self


class FixedJudge(DeliberationBaseModel):
    """Fixed-rubric judge result (§8.2, OSS shape — verbatim contract)."""

    verdict: Verdict
    rubric_id: str = "std64_fixed_judge_v1"
    reason: str


class DisagreementResult(DeliberationBaseModel):
    """Scorer-v0 output (§7): rule-based classification + explicit escalation."""

    classification: DisagreementClass
    max_verdict_distance: int = 0
    reason_code_overlap: float = Field(default=1.0, ge=0.0, le=1.0)
    risk_flag_overlap: float = Field(default=1.0, ge=0.0, le=1.0)
    max_confidence_gap: int = 0
    escalate: bool = False


class DissentRecord(DeliberationBaseModel):
    """Preserved minority position (§8.1) — verbatim, never averaged away."""

    node_id: str
    model_card: str
    verdict: Verdict
    reason_codes: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    summary: str = ""


class PartialResult(DeliberationBaseModel):
    """Partial-result shape required on every non-``complete`` receipt (§9)."""

    completed_nodes: list[str] = Field(default_factory=list)
    missing_nodes: list[str] = Field(default_factory=list)
    usable_intermediate_outputs: list[str] = Field(
        default_factory=list,
        description="Summaries / artifact foreign-references only (§8.4) — never raw traces",
    )
    recommended_resume_mode: str | None = None
    spend_guard_reason: str | None = None


class DeliberationReceipt(DeliberationBaseModel):
    """Deliberation Receipt (§8 OSS shape).

    References to monitor.db / journal.db records are foreign-reference only
    (``decision_ref`` / ``context_package_ref`` / ``correlation_id``) — no
    payload duplication. Public-safe by default.
    """

    receipt_id: str = Field(description='"dr_<uuid>"')
    correlation_id: str = Field(
        description="Stable identifier for audit linkage to the originating "
        "invocation/session/request/Context Package (§8.1)"
    )
    decision_ref: str = Field(description="Foreign reference to the decision under review")
    risk_class: str = Field(description="Policy §15.1 risk class (e.g. 'standards-change')")
    mode: Mode = "advisory"

    providers_consulted: list[str] = Field(
        default_factory=list, description="Model cards of the perspectives consulted"
    )
    node_outputs: list[NodeOutput] = Field(default_factory=list)
    disagreement: DisagreementResult | None = None
    dissent: list[DissentRecord] = Field(default_factory=list)

    synthesis: str = ""
    recommendation: str = ""
    fixed_judge: FixedJudge | None = None
    fallback_reason: str | None = Field(
        default=None,
        description="Set when the deterministic fallback fired (§5.1) — e.g. judge "
        "inconclusive/unavailable after max passes",
    )

    adjudication: Adjudication | None = None
    adjudicated_by: str | None = None

    result_state: ResultState = "complete"
    partial: PartialResult | None = None

    context_package_ref: str | None = None
    created_at: str = Field(description="ISO-8601 UTC timestamp")
    engine_version: str

    extensions: dict[str, Any] = Field(
        default_factory=dict,
        description="Namespaced extension envelope; Pro numeric scoring fields live "
        "under extensions['tokenpak_paid'] ONLY (§8.3)",
    )

    @model_validator(mode="after")
    def _partial_shape_on_non_complete(self) -> "DeliberationReceipt":
        if self.result_state != "complete" and self.partial is None:
            raise ValueError(
                "non-'complete' receipts must carry the partial-result shape (policy §9)"
            )
        return self
