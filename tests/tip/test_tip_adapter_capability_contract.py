# SPDX-License-Identifier: Apache-2.0
"""Tests for the TIP adapter capability contract.

Covers version parsing, the fail-loud compatibility self-test, capability
label validation, public-safe error messages, and the registry startup
self-test that gates out incompatible adapters before they can route traffic.
"""

from __future__ import annotations

import pytest

from tokenpak.tip.adapter_contract import (
    ASSERTED_TIP_VERSION,
    AdapterCapabilityContract,
    AdapterCompatibilityError,
    TipVersion,
    is_known_capability,
    validate_adapter_compatibility,
)
from tokenpak.tip.capabilities import TIP_COMPRESSION_V1


def test_tip_version_parse_concrete_and_wildcard():
    v = TipVersion.parse("TIP-1.0")
    assert (v.major, v.minor) == (1, 0)
    w = TipVersion.parse("TIP-1.x")
    assert (w.major, w.minor) == (1, None)


@pytest.mark.parametrize(
    "bad", ["1.0", "TIP-1", "TIP-1.2.3", "TIP-x.0", "", "TIP-1.y", "TIP-1.-1"]
)
def test_tip_version_parse_rejects_malformed(bad):
    with pytest.raises(AdapterCompatibilityError):
        TipVersion.parse(bad)


def test_compatible_adapter_passes_and_exposes_capabilities():
    contract = AdapterCapabilityContract(
        adapter_name="demo",
        capabilities=frozenset({TIP_COMPRESSION_V1}),
    )
    # default min/max bracket the asserted version -> no raise
    validate_adapter_compatibility(contract)
    contract.validate()
    assert TIP_COMPRESSION_V1 in contract.capabilities


def test_incompatible_min_version_fails():
    contract = AdapterCapabilityContract(adapter_name="future", tip_min_version="TIP-2.0")
    with pytest.raises(AdapterCompatibilityError):
        validate_adapter_compatibility(contract)


def test_incompatible_max_version_fails():
    contract = AdapterCapabilityContract(adapter_name="legacy", tip_max_version="TIP-0.9")
    with pytest.raises(AdapterCompatibilityError):
        validate_adapter_compatibility(contract)


def test_unknown_capability_label_fails():
    contract = AdapterCapabilityContract(
        adapter_name="weird",
        capabilities=frozenset({"tip.not.a.real.label"}),
    )
    with pytest.raises(AdapterCompatibilityError):
        validate_adapter_compatibility(contract)


def test_vendor_ext_capability_is_known():
    assert is_known_capability("ext.acme.turbo")
    assert not is_known_capability("ext.")
    assert not is_known_capability("")
    contract = AdapterCapabilityContract(
        adapter_name="vendor",
        capabilities=frozenset({"ext.acme.turbo"}),
    )
    validate_adapter_compatibility(contract)  # no raise


def test_wildcard_max_brackets_current_asserted_version():
    contract = AdapterCapabilityContract(
        adapter_name="wide", tip_min_version="TIP-1.0", tip_max_version="TIP-1.x"
    )
    validate_adapter_compatibility(contract, ASSERTED_TIP_VERSION)


def test_error_message_is_public_safe():
    contract = AdapterCapabilityContract(adapter_name="future", tip_min_version="TIP-2.0")
    with pytest.raises(AdapterCompatibilityError) as ei:
        validate_adapter_compatibility(contract)
    msg = str(ei.value)
    assert "/home/" not in msg
    assert "/Users/" not in msg
    assert "future" in msg


def test_registry_self_test_gates_out_incompatible_adapter():
    from tokenpak.proxy.adapters.passthrough_adapter import PassthroughAdapter
    from tokenpak.proxy.adapters.registry import AdapterRegistry

    class _IncompatibleAdapter(PassthroughAdapter):
        source_format = "incompatible"
        tip_min_version = "TIP-2.0"  # requires a newer TIP than the proxy asserts

    reg = AdapterRegistry()
    reg.register(PassthroughAdapter(), priority=0)
    reg.register(_IncompatibleAdapter(), priority=10)

    result = reg.run_startup_self_test()

    formats = reg.list_formats()
    assert "incompatible" not in formats  # gated out before it can route
    assert "passthrough" in formats
    assert not result.ok
    assert any(fmt == "incompatible" for fmt, _ in result.gated_out)
    assert "passthrough" in result.passed
