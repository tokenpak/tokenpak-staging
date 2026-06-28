"""provider_adapter compile-target contract tests (Std 54 §D / Std 23 §2).

Pins the gap this packet closed: provider_adapter cards compile a
``target_contract`` stamp of ``tokenpak.services.routing_service.CredentialProvider``,
which previously resolved to nothing (the contract existed only on the
archived ``canonical-main`` lineage). These tests prove the stamp resolves
end-to-end against the freshly authored contract and pin its Std 23
invariants.
"""

from __future__ import annotations

import importlib

import pytest

from tokenpak.cards.compile import derive_provider_class_name
from tokenpak.cards.model import TARGET_CONTRACT_PROVIDER_ADAPTER
from tokenpak.services.routing_service import CredentialProvider, InjectionPlan

# ---------------------------------------------------------------------------
# The compile target resolves (the dangling-pointer regression)
# ---------------------------------------------------------------------------


def test_target_contract_constant_resolves_to_credential_provider():
    """Std 54 §D: the stamped target_contract is importable, not a dangling name."""
    module_path, _, attr = TARGET_CONTRACT_PROVIDER_ADAPTER.rpartition(".")
    module = importlib.import_module(module_path)
    resolved = getattr(module, attr)
    assert resolved is CredentialProvider


def test_contract_lives_at_the_std23_module_path():
    """Std 23 §2 names credential_injector.py as the contract home."""
    module = importlib.import_module("tokenpak.services.routing_service.credential_injector")
    assert module.CredentialProvider is CredentialProvider
    assert module.InjectionPlan is InjectionPlan


def test_derived_provider_class_names_follow_std23_convention():
    assert derive_provider_class_name("acme-llm") == "AcmeLlmCredentialProvider"
    assert derive_provider_class_name("tokenpak-mistral") == ("TokenpakMistralCredentialProvider")


# ---------------------------------------------------------------------------
# CredentialProvider protocol (Std 23 §1 / §2.3)
# ---------------------------------------------------------------------------


class _ConformingProvider:
    name = "tokenpak-acme-llm"

    def resolve(self) -> InjectionPlan | None:
        return InjectionPlan(add_headers={"authorization": "Bearer test"})


class _CredlessProvider:
    name = "tokenpak-acme-llm"

    def resolve(self) -> InjectionPlan | None:
        return None  # graceful skip — the §2.3 missing-creds contract


class _NonConforming:
    pass


def test_protocol_runtime_checkable():
    assert isinstance(_ConformingProvider(), CredentialProvider)
    assert isinstance(_CredlessProvider(), CredentialProvider)
    assert not isinstance(_NonConforming(), CredentialProvider)


def test_resolve_none_is_the_missing_creds_contract():
    assert _CredlessProvider().resolve() is None


# ---------------------------------------------------------------------------
# InjectionPlan slots + invariants (Std 23 §2.1 / §2.3 / §2.5)
# ---------------------------------------------------------------------------


def test_plan_defaults_are_a_no_op_description():
    plan = InjectionPlan()
    assert plan.strip_headers == frozenset()
    assert dict(plan.add_headers) == {}
    assert dict(plan.merge_headers) == {}
    assert plan.target_url_override is None
    assert plan.target_url_resolver is None
    assert plan.body_transform is None
    assert plan.header_resolver is None
    assert plan.request_shape == "http"


def test_plan_is_frozen():
    plan = InjectionPlan()
    with pytest.raises(AttributeError):
        plan.target_url_override = "https://example.invalid"  # type: ignore[misc]


def test_strip_headers_normalized_to_lowercase():
    plan = InjectionPlan(strip_headers=frozenset({"X-Api-Key", "authorization"}))
    assert plan.strip_headers == frozenset({"x-api-key", "authorization"})


def test_ws_upgrade_with_body_transform_rejected_at_plan_load():
    """Std 23 §2.5: this combination MUST be rejected at plan-load time."""
    with pytest.raises(ValueError, match="ws-upgrade"):
        InjectionPlan(body_transform=lambda b: b, request_shape="ws-upgrade")


def test_http_with_body_transform_is_fine():
    plan = InjectionPlan(body_transform=lambda b: b, request_shape="http")
    assert plan.body_transform is not None


def test_sse_upgrade_reserved_shape_accepted():
    plan = InjectionPlan(request_shape="sse-upgrade")
    assert plan.request_shape == "sse-upgrade"
