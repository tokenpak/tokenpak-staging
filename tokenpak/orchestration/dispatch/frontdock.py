"""FrontDock intake module — request → DispatchJob (+ Manifest draft, optional Decision).

FrontDock is the single intake stage of TokenPak Dispatch (Standards Delta v0 §4;
convergence round-5 §1: there is **no** separate "Intake Strategist" worker —
FrontDock is a module, NOT a worker). It turns a raw request into the records the
downstream runtime consumes:

* a :class:`DispatchJob` (the §4.1 intake record),
* a **draft** :class:`DispatchManifest` (the §4.2 scoped work contract), and
* an **optional** blocking :class:`DispatchDecision` (the §4.6 Decision Inbox card).

Two layers, matching the §4 acceptance criteria:

* **Deterministic rules first.** Intent detection, route hints, risk-flag tagging,
  assumption/acceptance drafting, and missing-information detection are computed by
  pure deterministic rules whenever the request is unambiguous. No LLM call is made
  on this path.
* **One LLM call (through TIP) only when judgment is required.** When the
  deterministic intent classifier is inconclusive, FrontDock makes **exactly one**
  call via an *injected* client (the :class:`FrontDockLLM` protocol) to obtain the
  intake judgment, then schema-validates it fail-loud. The dispatch runtime that
  binds the real TIP path is a later packet; like :class:`~..stations.reviewer.
  ReviewerStation`, FrontDock depends only on the thin injected contract so it never
  imports a provider directly ("LLM calls go through TIP. No direct provider calls.").

**The Front Dock Rule** (round-1 §1): ask only for information that *materially
changes the outcome*. FrontDock therefore surfaces ``missing_info`` for high-risk
items only, and — per §4 — when any missing item is high-risk it creates a blocking
:class:`DispatchDecision` and **never assumes silently** for that item. Low-risk gaps
are recorded as drafted ``assumptions`` instead of blocking the user.

FrontDock does NOT select the route (that is P-RUNTIME-01 / §8): it emits a
``route_hint`` only. It does not run any station.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, Union, runtime_checkable

from pydantic import Field, ValidationError

from .models.common import (
    AcceptanceCriterion,
    DispatchBaseModel,
    ManifestPermissions,
    PathPolicy,
    QualityRequirements,
)
from .models.decision import (
    DecisionDefaultAction,
    DecisionOption,
    DecisionRecommendation,
    DispatchDecision,
)
from .models.enums import (
    AutoApplyAfter,
    AutonomyMode,
    DecisionScope,
    DecisionStatus,
    DispatchJobStatus,
    ManifestStatus,
    RiskLevel,
)
from .models.job import DispatchJob
from .models.manifest import DispatchManifest

# Stable template id so the prompt surface is greppable / versionable without a
# prose blob duplicated elsewhere (mirrors the Reviewer Station, §5.7).
INTAKE_PROMPT_TEMPLATE_ID = "dispatch.frontdock.intake.v1"

# Risk levels that materially change the outcome and therefore must never be
# silently assumed (Front Dock Rule + §4: blocking decision on high-risk gaps).
HIGH_RISK_LEVELS: frozenset[RiskLevel] = frozenset({RiskLevel.HIGH, RiskLevel.CRITICAL})

# Numeric ordering for RiskLevel so the blocking decision can carry the *highest*
# risk among its triggering items. Single source of truth.
_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


# ---------------------------------------------------------------------------
# Intent + route-hint registry (deterministic layer)
# ---------------------------------------------------------------------------

# Recognised intents (DispatchJob.detected_intent is registry-bound, §4.1).
INTENT_CODE_TASK = "code_task"
INTENT_DOC_TASK = "doc_task"
INTENT_QUICK_ANSWER = "quick_answer"
INTENT_UNKNOWN = "unknown"

# Deterministic intent → route_hint map. FrontDock emits the hint only; route
# *selection* is P-RUNTIME-01 (§8). ``unknown`` carries no hint.
INTENT_ROUTE_HINTS: dict[str, str | None] = {
    INTENT_CODE_TASK: "route.code_task.v1",
    INTENT_DOC_TASK: "route.doc_task.v1",
    INTENT_QUICK_ANSWER: "route.quick_answer.v1",
    INTENT_UNKNOWN: None,
}

# Keyword signals per intent (lower-cased, word-boundary matched). Scored: the
# intent with the strictly-highest distinct-signal count wins; a zero or tied
# score is inconclusive and triggers the LLM fallback.
_INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    INTENT_CODE_TASK: (
        "code", "implement", "fix", "bug", "refactor", "function", "class",
        "module", "patch", "build", "feature", "endpoint", "compile", "deploy",
    ),
    INTENT_DOC_TASK: (
        "document", "documentation", "docs", "readme", "guide", "changelog",
        "tutorial", "write-up", "writeup", "docstring",
    ),
    INTENT_QUICK_ANSWER: (
        "what", "why", "when", "who", "which", "explain", "define",
        "difference", "meaning",
    ),
}

# Deterministic risk registry (PAKPlan risk_flag registry — kept as free strings
# at v0.1-alpha, see §4 + the Reviewer's ReviewerRiskFlag note). Each entry maps
# a flag id to its keyword signals and the risk level it carries.
_RISK_RULES: tuple[tuple[str, RiskLevel, tuple[str, ...]], ...] = (
    ("destructive_operation", RiskLevel.HIGH,
     ("delete", "rm -rf", "drop table", "truncate", "wipe", "destroy", "purge")),
    ("touches_production", RiskLevel.HIGH,
     ("production", "prod ", "live system", "live database", "customer data")),
    ("handles_secrets", RiskLevel.HIGH,
     ("secret", "credential", "api key", "password", "private key", "access token")),
    ("history_rewrite", RiskLevel.HIGH,
     ("force push", "force-push", "push -f", "rewrite history", "reset --hard")),
    ("schema_migration", RiskLevel.MEDIUM,
     ("migration", "alter table", "schema change", "drop column", "backfill")),
)


# ---------------------------------------------------------------------------
# Internal judgment shape (shared by both the deterministic and LLM paths)
# ---------------------------------------------------------------------------


class MissingInfoItem(DispatchBaseModel):
    """A piece of missing information, with the risk of guessing it wrong.

    Per the Front Dock Rule, FrontDock only records items that *materially change
    the outcome*. An item whose ``risk_level`` is high/critical is never assumed
    silently — it forces a blocking :class:`DispatchDecision`.
    """

    description: str
    risk_level: RiskLevel = RiskLevel.LOW


class IntakeJudgment(DispatchBaseModel):
    """The intake judgment FrontDock assembles records from.

    Produced deterministically when the request is unambiguous, or parsed from a
    single injected LLM call when judgment is required. Both paths converge here so
    record assembly (:meth:`FrontDock._assemble`) is identical regardless of source.
    """

    detected_intent: str
    route_hint: str | None = None
    goal: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    missing_info: list[MissingInfoItem] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)


class IntentMatch(DispatchBaseModel):
    """Result of deterministic intent detection.

    ``conclusive`` is ``True`` when a single intent clearly won the keyword scoring;
    a ``False`` value is what routes intake to the LLM fallback.
    """

    intent: str
    route_hint: str | None
    conclusive: bool
    scores: dict[str, int] = Field(default_factory=dict)


class FrontDockResult(DispatchBaseModel):
    """The full output of one intake: the job, the manifest draft, optional decision."""

    job: DispatchJob
    manifest: DispatchManifest
    decision: DispatchDecision | None = None
    used_llm: bool = False


# ---------------------------------------------------------------------------
# Injected LLM client contract + fail-loud error (mirrors the Reviewer Station)
# ---------------------------------------------------------------------------


@runtime_checkable
class FrontDockLLM(Protocol):
    """Injected single-call intake client (routes through TIP at runtime).

    The dispatch runtime (TIP worker invocation) is a later packet; FrontDock
    depends only on this thin contract so it can be exercised with a fake client in
    tests and bound to the real TIP path once the runner lands. The callable takes
    the rendered intake prompt and returns the model's raw output — either a JSON
    string or an already-parsed mapping. **Exactly one** call is made, and only on
    the judgment-required (fallback) path.
    """

    def __call__(self, prompt: str) -> Union[str, dict[str, Any]]: ...


class FrontDockOutputError(ValueError):
    """Raised when the injected client returns output that is not a valid judgment.

    Covers non-JSON strings, non-mapping payloads, and payloads that fail
    :class:`IntakeJudgment` schema validation. Subclasses :class:`ValueError` so
    callers can catch it broadly while still matching it by exact type.
    """


class FrontDockLLMRequiredError(RuntimeError):
    """Raised when judgment is required but no LLM client was injected.

    FrontDock can be constructed without a client for purely deterministic intake;
    if such an instance then meets an ambiguous request it fails loud rather than
    guessing an intent (a guess would violate the Front Dock Rule).
    """


# ---------------------------------------------------------------------------
# FrontDock
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-]*")

# Per-intent draft templates for the deterministic path. These are conservative
# starting points the downstream stages refine; they are NOT a substitute for the
# LLM judgment used on ambiguous requests.
_ASSUMPTION_TEMPLATES: dict[str, tuple[str, ...]] = {
    INTENT_CODE_TASK: (
        "Work targets the current repository working tree.",
        "Existing code style, lint, and test conventions apply.",
    ),
    INTENT_DOC_TASK: (
        "Documentation follows the repository's existing docs structure and tone.",
    ),
    INTENT_QUICK_ANSWER: (
        "A concise, self-contained answer is sufficient; no repository changes are made.",
    ),
    INTENT_UNKNOWN: (),
}
_ACCEPTANCE_TEMPLATES: dict[str, tuple[str, ...]] = {
    INTENT_CODE_TASK: (
        "The requested code change is implemented.",
        "Tests covering the change pass.",
    ),
    INTENT_DOC_TASK: (
        "The requested documentation is written and accurate.",
    ),
    INTENT_QUICK_ANSWER: (
        "The question is answered correctly and directly.",
    ),
    INTENT_UNKNOWN: (),
}
# Quality requirements per intent (§4.2 quality_requirements block).
_QUALITY_BY_INTENT: dict[str, dict[str, bool]] = {
    INTENT_CODE_TASK: dict(
        test_required=True, review_required=True, docs_required=False, evidence_required=True
    ),
    INTENT_DOC_TASK: dict(
        test_required=False, review_required=True, docs_required=True, evidence_required=True
    ),
    INTENT_QUICK_ANSWER: dict(
        test_required=False, review_required=False, docs_required=False, evidence_required=False
    ),
    INTENT_UNKNOWN: dict(
        test_required=False, review_required=True, docs_required=False, evidence_required=True
    ),
}

# Caller-hint → AutonomyMode interpretation (§14.2 default-by-caller table).
_AUTONOMY_ALIASES: dict[str, AutonomyMode] = {
    "cli": AutonomyMode.DISPATCH_WITH_APPROVAL,
    "bare": AutonomyMode.DISPATCH_WITH_APPROVAL,
    "--ci": AutonomyMode.AUTO_DISPATCH_LIMITED,
    "ci": AutonomyMode.AUTO_DISPATCH_LIMITED,
    "--dry-run": AutonomyMode.DRAFT,
    "dry-run": AutonomyMode.DRAFT,
    "dry_run": AutonomyMode.DRAFT,
}


class FrontDock:
    """Single-stage intake: raw request → DispatchJob (+ Manifest draft, optional Decision).

    Construct with an optional injected :class:`FrontDockLLM`. :meth:`intake` runs
    deterministic detection first and only calls the client (exactly once) when the
    intent is inconclusive. ``id_factory`` and ``now`` are injectable for
    deterministic tests; they default to a ULID-style id and a UTC timestamp.
    """

    template_id = INTAKE_PROMPT_TEMPLATE_ID

    def __init__(
        self,
        client: FrontDockLLM | None = None,
        *,
        id_factory: Callable[[str], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._id_factory = id_factory or _default_id
        self._now = now or (lambda: datetime.now(timezone.utc))

    # -- public API ---------------------------------------------------------

    def intake(
        self,
        raw_request: str,
        *,
        autonomy_mode: AutonomyMode | str | None = None,
        source_task_packet_id: str | None = None,
    ) -> FrontDockResult:
        """Run one intake and return the assembled records.

        Deterministic intent detection runs first; if it is inconclusive a single
        LLM call (via the injected client) produces the judgment. The judgment is
        then assembled into a :class:`DispatchJob`, a draft
        :class:`DispatchManifest`, and — when any missing item is high-risk — a
        blocking :class:`DispatchDecision`.
        """

        if not raw_request or not raw_request.strip():
            raise ValueError("raw_request must be a non-empty string.")

        resolved_autonomy = self._resolve_autonomy(autonomy_mode)
        match = self.detect_intent(raw_request)

        if match.conclusive:
            judgment = self._deterministic_judgment(raw_request, match)
            used_llm = False
        else:
            judgment = self._judge_via_llm(raw_request, match)
            used_llm = True

        # Deterministic risk-flag tagging is a safety floor that runs on BOTH
        # paths: "never assume silently for high-risk" (§4 / Front Dock Rule) must
        # not depend on the LLM noticing the risk. This merges the deterministic
        # flags + high-risk confirmation items into whatever judgment we have.
        self._apply_risk_floor(raw_request, judgment)

        result = self._assemble(
            raw_request=raw_request,
            judgment=judgment,
            autonomy_mode=resolved_autonomy,
            source_task_packet_id=source_task_packet_id,
        )
        result.used_llm = used_llm
        return result

    # -- deterministic intent detection -------------------------------------

    def detect_intent(self, raw_request: str) -> IntentMatch:
        """Score intent keyword signals; return a (possibly inconclusive) match.

        An intent is conclusive only when exactly one intent has the strictly
        highest non-zero distinct-signal count. Zero hits or a tie is inconclusive,
        which routes intake to the LLM fallback rather than guessing.
        """

        text = raw_request.lower()
        words = set(_WORD_RE.findall(text))
        scores: dict[str, int] = {}
        for intent, keywords in _INTENT_KEYWORDS.items():
            score = 0
            for kw in keywords:
                # Multi-word / punctuated signals are matched as substrings; plain
                # tokens are matched on word boundaries to avoid false positives.
                if " " in kw or "-" in kw:
                    if kw in text:
                        score += 1
                elif kw in words:
                    score += 1
            scores[intent] = score

        best_intent = max(scores, key=lambda k: scores[k])
        best_score = scores[best_intent]
        # Conclusive iff non-zero and strictly greater than every other intent.
        conclusive = best_score > 0 and all(
            best_score > other for k, other in scores.items() if k != best_intent
        )
        intent = best_intent if conclusive else INTENT_UNKNOWN
        return IntentMatch(
            intent=intent,
            route_hint=INTENT_ROUTE_HINTS.get(intent),
            conclusive=conclusive,
            scores=scores,
        )

    def detect_risk_flags(self, raw_request: str) -> list[tuple[str, RiskLevel]]:
        """Deterministically tag PAKPlan risk flags present in the request.

        Returns ``(flag_id, risk_level)`` pairs in registry order, de-duplicated.
        Free-string flag ids at v0.1-alpha (the PAKPlan registry itself is a
        separate concern, per §4).
        """

        text = raw_request.lower()
        flags: list[tuple[str, RiskLevel]] = []
        seen: set[str] = set()
        for flag_id, level, signals in _RISK_RULES:
            if flag_id in seen:
                continue
            if any(sig in text for sig in signals):
                flags.append((flag_id, level))
                seen.add(flag_id)
        return flags

    def _deterministic_judgment(
        self, raw_request: str, match: IntentMatch
    ) -> IntakeJudgment:
        """Build the intent-derived judgment from pure rules (no LLM call).

        Risk flags and high-risk missing_info are NOT computed here — the always-run
        :meth:`_apply_risk_floor` owns that so both the deterministic and LLM paths
        get the identical safety floor.
        """

        intent = match.intent
        return IntakeJudgment(
            detected_intent=intent,
            route_hint=match.route_hint,
            goal=raw_request.strip(),
            assumptions=list(_ASSUMPTION_TEMPLATES.get(intent, ())),
            missing_info=[],
            risk_flags=[],
            acceptance_criteria=list(_ACCEPTANCE_TEMPLATES.get(intent, ())),
        )

    def _apply_risk_floor(self, raw_request: str, judgment: IntakeJudgment) -> None:
        """Merge the deterministic risk floor into ``judgment`` in place.

        Tags every deterministic PAKPlan risk flag (union with any the LLM already
        surfaced) and, for each high-risk flag, ensures a blocking confirmation item
        exists in ``missing_info`` so it is never assumed silently. Medium/low-risk
        flags are tagged but do not block (Front Dock Rule: only surface what
        materially changes the outcome).
        """

        for flag_id, level in self.detect_risk_flags(raw_request):
            if flag_id not in judgment.risk_flags:
                judgment.risk_flags.append(flag_id)
            if level not in HIGH_RISK_LEVELS:
                continue
            marker = f"({flag_id})"
            if any(marker in m.description for m in judgment.missing_info):
                continue
            judgment.missing_info.append(
                MissingInfoItem(
                    description=(
                        "Explicit confirmation required before proceeding: the request "
                        f"appears to involve a high-risk operation ({flag_id}). Confirm "
                        "the exact target and intent."
                    ),
                    risk_level=level,
                )
            )

    # -- LLM fallback path --------------------------------------------------

    def build_prompt(self, raw_request: str, match: IntentMatch) -> str:
        """Render the intake prompt (deterministic; no I/O, no client call).

        Embeds the raw request, the deterministic scoring FrontDock already
        computed, the recognised intent/route registry, and the exact JSON shape
        the model must return — derived from the live :class:`IntakeJudgment` schema
        so the instruction can never drift from the contract.
        """

        request = {
            "template_id": self.template_id,
            "task": (
                "You are the intake stage of a dispatch system. Classify the request "
                "and draft the intake judgment. Return ONLY a JSON object matching the "
                "provided judgment schema; no prose outside the JSON. detected_intent "
                "must be one of the recognised intents. Apply the Front Dock Rule: only "
                "list missing_info that materially changes the outcome, and mark an "
                "item's risk_level high or critical when guessing it wrong would be "
                "unsafe — those will block on a user decision rather than be assumed."
            ),
            "raw_request": raw_request,
            "recognised_intents": sorted(INTENT_ROUTE_HINTS),
            "route_hints": INTENT_ROUTE_HINTS,
            "deterministic_intent_scores": match.scores,
            "judgment_schema": IntakeJudgment.model_json_schema(),
        }
        return json.dumps(request, sort_keys=True)

    def _judge_via_llm(self, raw_request: str, match: IntentMatch) -> IntakeJudgment:
        """Make exactly one client call and validate the judgment fail-loud."""

        if self._client is None:
            raise FrontDockLLMRequiredError(
                "intent is ambiguous and judgment is required, but no FrontDockLLM "
                "client was injected; refusing to guess an intent (Front Dock Rule)."
            )
        prompt = self.build_prompt(raw_request, match)
        raw = self._client(prompt)  # exactly one LLM call, fallback path only
        parsed = self._coerce_to_mapping(raw)
        judgment = self._validate(parsed)
        # Backfill a route hint from the recognised registry when the model
        # classified an intent but left the hint null.
        if judgment.route_hint is None:
            judgment.route_hint = INTENT_ROUTE_HINTS.get(judgment.detected_intent)
        return judgment

    @staticmethod
    def _coerce_to_mapping(raw: Union[str, dict[str, Any]]) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                decoded = json.loads(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                raise FrontDockOutputError(
                    f"intake client returned non-JSON output: {exc}"
                ) from exc
            if not isinstance(decoded, dict):
                raise FrontDockOutputError(
                    "intake client returned a JSON value that is not an object "
                    f"(got {type(decoded).__name__})."
                )
            return decoded
        raise FrontDockOutputError(
            "intake client must return a JSON string or a mapping; got "
            f"{type(raw).__name__}."
        )

    @staticmethod
    def _validate(parsed: dict[str, Any]) -> IntakeJudgment:
        try:
            return IntakeJudgment.model_validate(parsed)
        except ValidationError as exc:
            raise FrontDockOutputError(
                f"intake judgment failed schema validation (§4): {exc}"
            ) from exc

    # -- assembly -----------------------------------------------------------

    def _assemble(
        self,
        *,
        raw_request: str,
        judgment: IntakeJudgment,
        autonomy_mode: AutonomyMode,
        source_task_packet_id: str | None,
    ) -> FrontDockResult:
        """Assemble the DispatchJob, draft DispatchManifest, and optional Decision."""

        created_at = self._now()
        job_id = self._id_factory("job")

        high_risk_missing = [
            m for m in judgment.missing_info if m.risk_level in HIGH_RISK_LEVELS
        ]

        decision: DispatchDecision | None = None
        if high_risk_missing:
            decision = self._build_blocking_decision(
                job_id=job_id, items=high_risk_missing, created_at=created_at
            )

        job = DispatchJob(
            id=job_id,
            created_at=created_at,
            raw_request=raw_request,
            source_task_packet_id=source_task_packet_id,
            detected_intent=judgment.detected_intent,
            route_hint=judgment.route_hint,
            assumptions=list(judgment.assumptions),
            missing_info=[m.description for m in judgment.missing_info],
            risk_flags=list(judgment.risk_flags),
            autonomy_mode=autonomy_mode,
            status=DispatchJobStatus.DRAFT,
        )

        manifest = DispatchManifest(
            id=self._id_factory("manifest"),
            job_id=job_id,
            route_id=judgment.route_hint or "route.unspecified.v1",
            goal=judgment.goal or raw_request.strip(),
            acceptance_criteria=[
                AcceptanceCriterion(id=f"ac{i + 1}", description=desc)
                for i, desc in enumerate(judgment.acceptance_criteria)
            ],
            constraints=[],
            deliverables=[],
            permissions=ManifestPermissions(autonomy_mode=autonomy_mode),
            path_policy=PathPolicy(),
            quality_requirements=QualityRequirements(
                **_QUALITY_BY_INTENT.get(
                    judgment.detected_intent, _QUALITY_BY_INTENT[INTENT_UNKNOWN]
                )
            ),
            # A high-risk gap means the manifest cannot be approved until the
            # blocking decision resolves; otherwise it is a plain draft.
            status=ManifestStatus.NEEDS_DECISION if decision else ManifestStatus.DRAFT,
        )

        return FrontDockResult(job=job, manifest=manifest, decision=decision)

    def _build_blocking_decision(
        self,
        *,
        job_id: str,
        items: list[MissingInfoItem],
        created_at: datetime,
    ) -> DispatchDecision:
        """Create the blocking Decision Card for high-risk missing info (§4.6).

        FrontDock never assumes silently for high-risk gaps; the safe default is to
        cancel rather than proceed without the confirmation (``auto_apply_after`` is
        ``never`` at v0.1-alpha regardless).
        """

        risk_level = max(items, key=lambda m: _RISK_ORDER[m.risk_level]).risk_level
        bullet_list = "\n".join(f"- {m.description}" for m in items)
        return DispatchDecision(
            id=self._id_factory("decision"),
            job_id=job_id,
            created_at=created_at,
            scope=DecisionScope.JOB,
            title="Confirmation required before dispatch",
            question=(
                "This request involves high-risk information that FrontDock will not "
                "assume on your behalf. Provide the missing details to proceed, or "
                "cancel the request:\n" + bullet_list
            ),
            reason=(
                "One or more missing items are high-risk; per the Front Dock Rule they "
                "materially change the outcome and must be confirmed explicitly."
            ),
            risk_level=risk_level,
            options=[
                DecisionOption(
                    id="provide",
                    label="Provide the missing details",
                    description="Supply the high-risk information so dispatch can proceed safely.",
                    tradeoffs=["Requires user input before any work starts."],
                ),
                DecisionOption(
                    id="cancel",
                    label="Cancel the request",
                    description="Do not dispatch; the high-risk details were not provided.",
                    tradeoffs=["The request is not fulfilled."],
                ),
            ],
            recommendation=DecisionRecommendation(
                option_id="provide",
                rationale=(
                    "High-risk items must be confirmed explicitly rather than guessed; "
                    "providing the details is safer than cancelling outright."
                ),
            ),
            default_action=DecisionDefaultAction(
                option_id="cancel", auto_apply_after=AutoApplyAfter.NEVER
            ),
            status=DecisionStatus.PENDING,
        )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _resolve_autonomy(mode: AutonomyMode | str | None) -> AutonomyMode:
        """Interpret an autonomy-mode hint (§4.1 + §14.2 default-by-caller).

        ``None`` and a bare CLI invocation default to ``dispatch_with_approval``;
        ``--ci`` → ``auto_dispatch_limited``; ``--dry-run`` → ``draft``. An exact
        :class:`AutonomyMode` member or canonical value string is honoured as-is.
        """

        if mode is None:
            return AutonomyMode.DISPATCH_WITH_APPROVAL
        if isinstance(mode, AutonomyMode):
            return mode
        key = mode.strip().lower()
        if key in _AUTONOMY_ALIASES:
            return _AUTONOMY_ALIASES[key]
        try:
            return AutonomyMode(key)
        except ValueError as exc:
            raise ValueError(
                f"unrecognised autonomy_mode hint {mode!r}; expected one of "
                f"{[m.value for m in AutonomyMode]} or a caller alias "
                f"{sorted(_AUTONOMY_ALIASES)}."
            ) from exc


def _default_id(prefix: str) -> str:
    """Default id factory: ``<prefix>_<uuid4 hex>`` (a ULID-style opaque id)."""

    return f"{prefix}_{uuid.uuid4().hex}"


__all__ = [
    "INTAKE_PROMPT_TEMPLATE_ID",
    "HIGH_RISK_LEVELS",
    "INTENT_CODE_TASK",
    "INTENT_DOC_TASK",
    "INTENT_QUICK_ANSWER",
    "INTENT_UNKNOWN",
    "INTENT_ROUTE_HINTS",
    "MissingInfoItem",
    "IntakeJudgment",
    "IntentMatch",
    "FrontDockResult",
    "FrontDockLLM",
    "FrontDockOutputError",
    "FrontDockLLMRequiredError",
    "FrontDock",
]
