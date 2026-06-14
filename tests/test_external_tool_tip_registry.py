# SPDX-License-Identifier: Apache-2.0
"""External-tool TIP source-adapter interface + registry tests (Std 23 §9).

Covers the packet acceptance criteria for the generic layer:

- registry is runtime-discovered, never an enum (feedback_always_dynamic)
- wiring a second tool requires ZERO core change (AC #5)
- off-by-default flag — unset flag is a strict no-op (AC #2)
- ext.* label conformance against the shipped TIP schema pattern
- tip.* labels rejected (reserved for protocol-native, Std 23 §9.2)
- provenance honesty contract: tokenpak-observed, never tool-native TIP
  (Std 23 §9.3)
"""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path
from typing import List

import pytest

from tokenpak.sources import external_tool_tip as ext
from tokenpak.sources.external_tool_tip import (
    ENV_EXTRA_MODULES,
    ENV_FLAG,
    ExternalToolTIPSource,
    ObservedTIPRecord,
    collect_observed_records,
    discover_sources,
    is_enabled,
    register_external_tool_source,
    registered_sources,
    unregister_external_tool_source,
    validate_ext_labels,
)


class _StubSource(ExternalToolTIPSource):
    tool_slug = "stubtool"
    static_capabilities = frozenset({"ext.stubtool.usage_observed"})

    def collect(self) -> List[ObservedTIPRecord]:
        return [
            ObservedTIPRecord(
                tool="stubtool",
                labels=["ext.stubtool.usage_observed"],
                session_id="s1",
            )
        ]


@pytest.fixture()
def clean_stub_registry():
    yield
    unregister_external_tool_source("stubtool")
    unregister_external_tool_source("othertool")


# ---------------------------------------------------------------------------
# Flag gating — off-by-default (AC #2; explicit unset-flag no-op)
# ---------------------------------------------------------------------------


def test_flag_unset_is_disabled(monkeypatch):
    monkeypatch.delenv(ENV_FLAG, raising=False)
    assert is_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", " 1 "])
def test_flag_truthy_values_enable(monkeypatch, value):
    monkeypatch.setenv(ENV_FLAG, value)
    assert is_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  "])
def test_flag_falsy_values_stay_disabled(monkeypatch, value):
    monkeypatch.setenv(ENV_FLAG, value)
    assert is_enabled() is False


def test_unset_flag_collect_is_noop(monkeypatch, clean_stub_registry):
    """With the flag unset, no adapter is discovered, instantiated, or run."""
    monkeypatch.delenv(ENV_FLAG, raising=False)

    class _BoobyTrap(ExternalToolTIPSource):
        tool_slug = "othertool"

        def __init__(self):
            raise AssertionError("adapter must never be instantiated when disabled")

        def collect(self):  # pragma: no cover - unreachable
            raise AssertionError("unreachable")

    register_external_tool_source(_BoobyTrap)
    result = collect_observed_records()
    assert result["skipped"] is True
    assert result["reason"] == "disabled"
    assert result["records"] == []
    assert result["sources"] == []


def test_unset_flag_cli_observe_is_noop(monkeypatch, capsys):
    """`tokenpak tip observe` without the flag: exit 0, disabled notice, no records."""
    import argparse

    from tokenpak.cli.commands.tip import cmd_tip_observe

    monkeypatch.delenv(ENV_FLAG, raising=False)
    rc = cmd_tip_observe(argparse.Namespace(json=True, tool=None))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] is True
    assert payload["records"] == []
    assert payload["flag"] == ENV_FLAG


# ---------------------------------------------------------------------------
# Registry — registration, not enumeration (feedback_always_dynamic)
# ---------------------------------------------------------------------------


def test_register_and_collect_roundtrip(monkeypatch, clean_stub_registry):
    monkeypatch.setenv(ENV_FLAG, "1")
    register_external_tool_source(_StubSource)
    assert registered_sources()["stubtool"] is _StubSource

    result = collect_observed_records(tool="stubtool")
    assert result["skipped"] is False
    assert "stubtool" in result["sources"]
    assert len(result["records"]) == 1
    assert result["records"][0].tool == "stubtool"


def test_register_is_decorator_compatible(clean_stub_registry):
    cls = register_external_tool_source(_StubSource)
    assert cls is _StubSource


def test_register_rejects_bad_slug():
    class _Bad(ExternalToolTIPSource):
        tool_slug = "Not A Slug!"

        def collect(self):
            return []

    with pytest.raises(ValueError, match="tool_slug"):
        register_external_tool_source(_Bad)


def test_register_rejects_tip_namespace_capabilities():
    class _Bad(ExternalToolTIPSource):
        tool_slug = "badtool"
        static_capabilities = frozenset({"tip.compression.v1"})

        def collect(self):
            return []

    with pytest.raises(ValueError, match="tip\\.\\* is reserved"):
        register_external_tool_source(_Bad)


def test_register_rejects_conflicting_duplicate(clean_stub_registry):
    register_external_tool_source(_StubSource)

    class _Other(ExternalToolTIPSource):
        tool_slug = "stubtool"

        def collect(self):
            return []

    with pytest.raises(ValueError, match="already registered"):
        register_external_tool_source(_Other)
    # idempotent re-registration of the same class is fine
    register_external_tool_source(_StubSource)


def test_no_hardcoded_tool_enum_in_module():
    """Guard: the registry module must not enumerate concrete tool slugs."""
    source_text = Path(ext.__file__).read_text(encoding="utf-8")
    assert "gstack" not in source_text, (
        "external_tool_tip.py must stay tool-agnostic — concrete tools "
        "register themselves (feedback_always_dynamic)"
    )


# ---------------------------------------------------------------------------
# AC #5 — second tool wires in with ZERO core change
# ---------------------------------------------------------------------------


def test_second_tool_no_core_change(tmp_path, monkeypatch, clean_stub_registry):
    """A brand-new tool registers via a dropped-in module + env var only."""
    module = tmp_path / "othertool_tip_source.py"
    module.write_text(textwrap.dedent(
        """
        from tokenpak.sources.external_tool_tip import (
            ExternalToolTIPSource,
            register_external_tool_source,
        )


        @register_external_tool_source
        class OtherToolTIPSource(ExternalToolTIPSource):
            tool_slug = "othertool"
            static_capabilities = frozenset({"ext.othertool.usage_observed"})

            def collect(self):
                return []
        """
    ), encoding="utf-8")

    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(ENV_EXTRA_MODULES, "othertool_tip_source")

    sources = discover_sources()
    assert "othertool" in sources
    # the in-tree gstack instance is discovered by the same mechanism
    assert "gstack" in sources


def test_discovery_tolerates_broken_module(monkeypatch, caplog):
    monkeypatch.setenv(ENV_EXTRA_MODULES, "definitely_not_a_real_module_xyz")
    with caplog.at_level("WARNING"):
        sources = discover_sources()
    assert "definitely_not_a_real_module_xyz" not in sources
    assert any("import-failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Label conformance — ext.* only, schema-pattern compliant (Std 23 §1.4/§9.2)
# ---------------------------------------------------------------------------


def _shipped_label_pattern() -> str:
    import tokenpak.tip as tip_pkg

    schema_path = (
        Path(tip_pkg.__file__).parent / "schemas" / "tip-capabilities.v1.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return schema["items"]["pattern"]


def test_ext_label_regex_is_subset_of_shipped_schema():
    shipped = re.compile(_shipped_label_pattern())
    for ok in ("ext.gstack.usage_observed", "ext.stubtool.phase.build"):
        assert ext.EXT_LABEL_RE.match(ok)
        assert shipped.match(ok)
    for bad in ("ext.", "EXT.gstack.x", "gstack.usage", "ext gstack"):
        assert not ext.EXT_LABEL_RE.match(bad)


def test_validate_rejects_tip_labels():
    with pytest.raises(ValueError, match="tip\\.\\* is reserved"):
        validate_ext_labels("stubtool", ["tip.compression.v1"])


def test_validate_rejects_foreign_namespace():
    with pytest.raises(ValueError, match="outside this tool's namespace"):
        validate_ext_labels("stubtool", ["ext.othertool.usage_observed"])


# ---------------------------------------------------------------------------
# Provenance honesty contract (Std 23 §9.3)
# ---------------------------------------------------------------------------


def test_record_defaults_to_tokenpak_observed():
    rec = ObservedTIPRecord(tool="stubtool", labels=["ext.stubtool.usage_observed"])
    assert rec.provenance["claim"] == "tokenpak-observed"
    assert rec.provenance["tool_native_tip"] is False
    assert rec.provenance["observed_by"] == "tokenpak"


def test_record_rejects_tool_native_tip_claim():
    with pytest.raises(ValueError, match="never claim"):
        ObservedTIPRecord(
            tool="stubtool",
            labels=["ext.stubtool.usage_observed"],
            provenance={"claim": "tokenpak-observed", "tool_native_tip": True},
        )


def test_record_rejects_wrong_claim():
    with pytest.raises(ValueError, match="honesty contract"):
        ObservedTIPRecord(
            tool="stubtool",
            labels=["ext.stubtool.usage_observed"],
            provenance={"claim": "gstack-native-tip"},
        )


def test_record_has_no_savings_fields():
    """Std 23 §9.3(2): no savings/percentage surface on observed records."""
    rec = ObservedTIPRecord(tool="stubtool", labels=["ext.stubtool.usage_observed"])
    payload = rec.to_dict()
    for key in payload:
        assert "savings" not in key.lower()
        assert "percent" not in key.lower()


def test_record_to_dict_is_json_serializable():
    rec = ObservedTIPRecord(
        tool="stubtool",
        labels=["ext.stubtool.usage_observed"],
        observed_usage={"input_tokens": 10, "output_tokens": 5},
        session_id="abc",
    )
    payload = json.loads(json.dumps(rec.to_dict()))
    assert payload["tool"] == "stubtool"
    assert payload["observed_usage"]["input_tokens"] == 10
    assert payload["record_id"].startswith("stubtool.")
