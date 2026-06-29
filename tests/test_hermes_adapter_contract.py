# SPDX-License-Identifier: Apache-2.0
"""Tests for the Hermes adapter contract stub.

The stub must declare a valid, compatible TIP contract yet be unable to route
real traffic, and the default registry must still pass the startup self-test
unchanged once the contract gate is in place.
"""

from __future__ import annotations

import pytest

from tokenpak.proxy.adapters.hermes_adapter import HermesAdapter
from tokenpak.tip.adapter_contract import validate_adapter_compatibility


def test_hermes_stub_declares_valid_contract():
    adapter = HermesAdapter()
    contract = adapter.capability_contract()
    assert contract.adapter_name == "hermes"
    assert contract.capabilities  # declares at least one capability
    # well-formed and compatible with the asserted TIP version -> no raise
    validate_adapter_compatibility(contract)


def test_hermes_stub_never_auto_detects():
    adapter = HermesAdapter()
    assert adapter.detect("/v1/messages", {}, b"{}") is False


def test_hermes_stub_cannot_route_traffic():
    adapter = HermesAdapter()
    with pytest.raises(NotImplementedError):
        adapter.normalize(b"{}")
    with pytest.raises(NotImplementedError):
        adapter.denormalize(object())  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        adapter.get_default_upstream()


def test_hermes_not_in_default_registry():
    from tokenpak.proxy.adapters import build_default_registry

    reg = build_default_registry()
    assert "hermes" not in reg.list_formats()


def test_default_registry_self_test_passes_unchanged():
    # Every default adapter must pass the contract gate; none gated out.
    from tokenpak.proxy.adapters import build_default_registry

    reg = build_default_registry()
    before = set(reg.list_formats())
    result = reg.run_startup_self_test()
    assert result.ok
    assert set(reg.list_formats()) == before
