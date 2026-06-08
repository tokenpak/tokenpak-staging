"""Tests for the FrontDock intake module (Standards Delta v0 §4).

Covers the four §4 acceptance-criteria test areas plus fail-loud handling:

  * deterministic-only intent detection (NO LLM call made);
  * the LLM fallback path (ambiguous request → exactly one injected call);
  * assumption drafting + acceptance-criteria drafting;
  * Decision Card creation triggers (high-risk missing_info → blocking
    DispatchDecision; never assume silently);
  * the Front Dock Rule (no missing_info / no decision for clear, low-risk asks);
  * autonomy-mode interpretation (§14.2 default-by-caller);
  * malformed LLM output fails loud.

A FAKE injected client is used throughout — no real LLM / TIP call.
"""

from __future__ import annotations

import json

import pytest

# Dispatch is pydantic-native; deps ship via the opt-in `dispatch` extra. Skip
# cleanly on slim installs that lack it rather than erroring at collection time.
pytest.importorskip("pydantic")

from tokenpak.orchestration.dispatch.frontdock import (
    INTENT_CODE_TASK,
    INTENT_DOC_TASK,
    INTENT_QUICK_ANSWER,
    INTENT_UNKNOWN,
    FrontDock,
    FrontDockLLMRequiredError,
    FrontDockOutputError,
)
from tokenpak.orchestration.dispatch.models.decision import DispatchDecision
from tokenpak.orchestration.dispatch.models.enums import (
    AutonomyMode,
    DispatchJobStatus,
    ManifestStatus,
    RiskLevel,
)
from tokenpak.orchestration.dispatch.models.job import DispatchJob
from tokenpak.orchestration.dispatch.models.manifest import DispatchManifest

# ---------------------------------------------------------------------------
# Fake injected client + deterministic FrontDock factory
# ---------------------------------------------------------------------------


class _FakeFrontDockLLM:
    """Records call count and returns a canned payload (str or dict)."""

    def __init__(self, payload, *, as_json_string: bool = True):
        self._payload = payload
        self._as_json_string = as_json_string
        self.calls = 0
        self.last_prompt = None

    def __call__(self, prompt: str):
        self.calls += 1
        self.last_prompt = prompt
        if isinstance(self._payload, (str, bytes)):
            return self._payload
        if self._as_json_string:
            return json.dumps(self._payload)
        return self._payload


class _CountingIds:
    """Deterministic, monotonic id factory so assertions are stable."""

    def __init__(self):
        self.counts: dict[str, int] = {}

    def __call__(self, prefix: str) -> str:
        self.counts[prefix] = self.counts.get(prefix, 0) + 1
        return f"{prefix}_{self.counts[prefix]:026d}"


def _frontdock(client=None) -> FrontDock:
    """A FrontDock with deterministic ids (no wall-clock / random in assertions)."""

    return FrontDock(client, id_factory=_CountingIds())


# ---------------------------------------------------------------------------
# Deterministic-only intent detection (no LLM call)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "request_text,expected_intent,expected_hint",
    [
        ("Implement a function to refactor the parser module", INTENT_CODE_TASK,
         "route.code_task.v1"),
        ("Write documentation and a README guide for the project", INTENT_DOC_TASK,
         "route.doc_task.v1"),
        ("What is the difference between a list and a tuple?", INTENT_QUICK_ANSWER,
         "route.quick_answer.v1"),
    ],
)
def test_deterministic_intent_detection_no_llm(request_text, expected_intent, expected_hint):
    client = _FakeFrontDockLLM("should not be called")
    fd = _frontdock(client)

    result = fd.intake(request_text)

    assert result.used_llm is False
    assert client.calls == 0  # deterministic path makes no LLM call
    assert isinstance(result.job, DispatchJob)
    assert result.job.detected_intent == expected_intent
    assert result.job.route_hint == expected_hint
    assert result.job.status is DispatchJobStatus.DRAFT


def test_deterministic_path_works_without_any_client():
    # No client injected at all; a clear request must still succeed deterministically.
    fd = _frontdock()
    result = fd.intake("Fix the bug in the build module")
    assert result.used_llm is False
    assert result.job.detected_intent == INTENT_CODE_TASK


# ---------------------------------------------------------------------------
# LLM fallback path
# ---------------------------------------------------------------------------


def _judgment_payload(intent: str = INTENT_CODE_TASK) -> dict:
    return {
        "detected_intent": intent,
        "route_hint": None,  # left null on purpose → FrontDock backfills from registry
        "goal": "do the ambiguous thing",
        "assumptions": ["assumed scope is the current repo"],
        "missing_info": [],
        "risk_flags": [],
        "acceptance_criteria": ["the ambiguous thing is done"],
    }


def test_llm_fallback_on_ambiguous_request():
    # A request with no recognised keyword signals is inconclusive → LLM fallback.
    client = _FakeFrontDockLLM(_judgment_payload(INTENT_CODE_TASK))
    fd = _frontdock(client)

    result = fd.intake("zorp the flibbet")

    assert result.used_llm is True
    assert client.calls == 1  # exactly one call on the fallback path
    assert result.job.detected_intent == INTENT_CODE_TASK
    # route_hint was null in the payload; FrontDock backfills it from the registry.
    assert result.job.route_hint == "route.code_task.v1"
    # Prompt embeds the raw request + the judgment schema (no client call to build it).
    assert "zorp the flibbet" in client.last_prompt
    assert "judgment_schema" in client.last_prompt


def test_ambiguous_request_without_client_fails_loud():
    fd = _frontdock()  # no client
    with pytest.raises(FrontDockLLMRequiredError):
        fd.intake("zorp the flibbet")


def test_llm_accepts_dict_payload_without_json_encoding():
    client = _FakeFrontDockLLM(_judgment_payload(), as_json_string=False)
    result = _frontdock(client).intake("zorp the flibbet")
    assert result.used_llm is True
    assert client.calls == 1


def test_clear_request_never_calls_llm_even_when_present():
    client = _FakeFrontDockLLM(_judgment_payload())
    fd = _frontdock(client)
    fd.intake("Implement the patch for the endpoint function")
    assert client.calls == 0


# ---------------------------------------------------------------------------
# Assumption + acceptance-criteria drafting
# ---------------------------------------------------------------------------


def test_assumption_and_acceptance_drafting_deterministic():
    fd = _frontdock()
    result = fd.intake("Implement the new feature in the module")

    assert result.job.assumptions, "code_task should draft assumptions"
    assert result.manifest.acceptance_criteria, "code_task should draft acceptance criteria"
    # Acceptance criteria are wrapped as AcceptanceCriterion with stable ids.
    ids = [ac.id for ac in result.manifest.acceptance_criteria]
    assert ids == [f"ac{i + 1}" for i in range(len(ids))]


def test_assumptions_flow_from_llm_judgment():
    payload = _judgment_payload()
    payload["assumptions"] = ["assumed A", "assumed B"]
    client = _FakeFrontDockLLM(payload)
    result = _frontdock(client).intake("zorp the flibbet")
    assert result.job.assumptions == ["assumed A", "assumed B"]


# ---------------------------------------------------------------------------
# Decision Card creation triggers (high-risk missing_info)
# ---------------------------------------------------------------------------


def test_high_risk_request_creates_blocking_decision():
    fd = _frontdock()
    # Clear intent ("implement"/"code" → code_task, conclusive, no LLM) AND
    # high-risk deterministic flags ("delete" + "production"). The risk floor runs
    # on the deterministic path, so no client is needed.
    result = fd.intake("Implement code to delete the production database tables")

    assert result.decision is not None
    assert isinstance(result.decision, DispatchDecision)
    assert result.decision.status.value == "pending"
    assert result.decision.risk_level is RiskLevel.HIGH
    # Never assume silently: the high-risk gaps are recorded as missing_info.
    assert result.job.missing_info
    # Manifest cannot be a plain draft while a blocking decision is pending.
    assert result.manifest.status is ManifestStatus.NEEDS_DECISION
    # Risk flags are tagged on the job.
    assert "destructive_operation" in result.job.risk_flags
    assert "touches_production" in result.job.risk_flags
    # The safe default action is to cancel, and v0.1-alpha never auto-applies.
    assert result.decision.default_action.option_id == "cancel"
    assert result.decision.default_action.auto_apply_after.value == "never"


def test_low_risk_request_creates_no_decision():
    # Front Dock Rule: a clear, low-risk ask must not block or over-ask.
    fd = _frontdock()
    result = fd.intake("Implement a helper function in the module")
    assert result.decision is None
    assert result.job.missing_info == []
    assert result.manifest.status is ManifestStatus.DRAFT


def test_high_risk_decision_via_llm_missing_info():
    payload = _judgment_payload()
    payload["missing_info"] = [
        {"description": "which environment?", "risk_level": "critical"},
        {"description": "cosmetic detail", "risk_level": "low"},
    ]
    client = _FakeFrontDockLLM(payload)
    result = _frontdock(client).intake("zorp the flibbet")

    assert result.decision is not None
    # The decision carries the highest risk among triggering items.
    assert result.decision.risk_level is RiskLevel.CRITICAL
    # Both missing items are recorded on the job (descriptions), low-risk included.
    assert "which environment?" in result.job.missing_info
    assert "cosmetic detail" in result.job.missing_info


def test_medium_risk_alone_does_not_block():
    # A medium-risk flag (schema_migration) is tagged but does not force a decision.
    fd = _frontdock()
    result = fd.intake("Write a migration to alter table users in the module")
    assert "schema_migration" in result.job.risk_flags
    assert result.decision is None


# ---------------------------------------------------------------------------
# Autonomy-mode interpretation (§14.2 default-by-caller)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hint,expected",
    [
        (None, AutonomyMode.DISPATCH_WITH_APPROVAL),
        ("cli", AutonomyMode.DISPATCH_WITH_APPROVAL),
        ("--ci", AutonomyMode.AUTO_DISPATCH_LIMITED),
        ("--dry-run", AutonomyMode.DRAFT),
        ("advisory", AutonomyMode.ADVISORY),
        (AutonomyMode.DRAFT, AutonomyMode.DRAFT),
    ],
)
def test_autonomy_mode_interpretation(hint, expected):
    fd = _frontdock()
    result = fd.intake("Implement the function in the module", autonomy_mode=hint)
    assert result.job.autonomy_mode is expected
    assert result.manifest.permissions.autonomy_mode is expected


def test_unknown_autonomy_hint_fails_loud():
    fd = _frontdock()
    with pytest.raises(ValueError):
        fd.intake("Implement the function", autonomy_mode="banana")


# ---------------------------------------------------------------------------
# Manifest draft + job/manifest linkage
# ---------------------------------------------------------------------------


def test_manifest_draft_is_linked_and_complete():
    fd = _frontdock()
    result = fd.intake("Implement the parser function")

    assert isinstance(result.manifest, DispatchManifest)
    assert result.manifest.job_id == result.job.id
    assert result.manifest.goal
    assert result.manifest.route_id == "route.code_task.v1"
    # PathPolicy always carries the four mandatory denied globs (safety floor).
    for glob in (".env", ".git/**", "secrets/**", "license/**"):
        assert glob in result.manifest.path_policy.denied_paths


def test_source_task_packet_id_crosswalk():
    fd = _frontdock()
    standalone = fd.intake("Implement the function")
    assert standalone.job.source_task_packet_id is None

    linked = fd.intake("Implement the function", source_task_packet_id="TASK-123")
    assert linked.job.source_task_packet_id == "TASK-123"


def test_empty_request_rejected():
    fd = _frontdock()
    with pytest.raises(ValueError):
        fd.intake("   ")


# ---------------------------------------------------------------------------
# Malformed LLM output → fail loud
# ---------------------------------------------------------------------------


def test_malformed_non_json_string_fails_loud():
    client = _FakeFrontDockLLM("this is not json")
    with pytest.raises(FrontDockOutputError):
        _frontdock(client).intake("zorp the flibbet")
    assert client.calls == 1


def test_malformed_json_array_not_object_fails_loud():
    client = _FakeFrontDockLLM("[1, 2, 3]")
    with pytest.raises(FrontDockOutputError):
        _frontdock(client).intake("zorp the flibbet")


def test_schema_invalid_payload_fails_loud():
    payload = _judgment_payload()
    payload["bogus_field"] = "nope"  # extra='forbid' → validation error
    client = _FakeFrontDockLLM(payload)
    with pytest.raises(FrontDockOutputError):
        _frontdock(client).intake("zorp the flibbet")


def test_missing_required_field_fails_loud():
    payload = _judgment_payload()
    del payload["detected_intent"]
    client = _FakeFrontDockLLM(payload)
    with pytest.raises(FrontDockOutputError):
        _frontdock(client).intake("zorp the flibbet")


def test_non_str_non_dict_client_output_fails_loud():
    client = _FakeFrontDockLLM(12345, as_json_string=False)
    with pytest.raises(FrontDockOutputError):
        _frontdock(client).intake("zorp the flibbet")


# ---------------------------------------------------------------------------
# build_prompt is pure (no client call)
# ---------------------------------------------------------------------------


def test_build_prompt_does_not_call_the_client():
    client = _FakeFrontDockLLM(_judgment_payload())
    fd = _frontdock(client)
    match = fd.detect_intent("zorp the flibbet")
    prompt = fd.build_prompt("zorp the flibbet", match)
    assert client.calls == 0
    assert "zorp the flibbet" in prompt
    assert "judgment_schema" in prompt
