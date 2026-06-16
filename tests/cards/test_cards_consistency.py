# SPDX-License-Identifier: Apache-2.0
"""Card→adapter consistency tests (Std 54 §H).

Contract under test:
    dev (default):  card ⊆ adapter required; adapter ⊃ card → warning
    strict/locked:  card == adapter required
    always:         overclaiming (card ⊄ adapter) blocked

The adapter module is read statically via ``ast.parse`` — these tests
also prove the adapter is never imported/executed.
"""

from __future__ import annotations

from pathlib import Path

from tokenpak.cards.parser import parse_card_file
from tokenpak.cards.validate import extract_adapter_capabilities, validate_card

CARD_TEMPLATE = """---
card_kind: tip
tip_kind: provider_adapter
name: acme-llm
target_contract: tokenpak.services.routing_service.CredentialProvider
source_format: tokenpak-card-md-1
card_status: local
tokenpak_min_version: "1.5.0"
capabilities: [{caps}]
---

# acme-llm
"""


def _write_pair(tmp_path: Path, card_caps: str, adapter_caps: str) -> Path:
    base = tmp_path / "integrations" / "acme-llm"
    base.mkdir(parents=True)
    card = base / "acme-llm.tip.md"
    card.write_text(CARD_TEMPLATE.format(caps=card_caps), encoding="utf-8")
    (base / "adapter.py").write_text(
        "# adapter module — must never be imported by the authoring layer\n"
        "raise RuntimeError('adapter module was imported — invariant broken')\n"
        f"capabilities = frozenset({{{adapter_caps}}})\n",
        encoding="utf-8",
    )
    return card


def test_subset_passes_in_dev_mode(tmp_path):
    card = _write_pair(
        tmp_path,
        card_caps="tip.compression.v1",
        adapter_caps="'tip.compression.v1', 'tip.cache.semantic.v1'",
    )
    report = validate_card(parse_card_file(card), mode="dev")
    assert report.ok
    # Adapter supporting more than the card declares → warning, not error.
    assert any(i.code == "capability-subset" for i in report.warnings)


def test_equality_passes_in_strict_mode(tmp_path):
    card = _write_pair(
        tmp_path,
        card_caps="tip.compression.v1",
        adapter_caps="'tip.compression.v1'",
    )
    report = validate_card(parse_card_file(card), strict=True)
    assert report.ok, [i.message for i in report.issues]


def test_subset_fails_in_strict_mode(tmp_path):
    card = _write_pair(
        tmp_path,
        card_caps="tip.compression.v1",
        adapter_caps="'tip.compression.v1', 'tip.cache.semantic.v1'",
    )
    report = validate_card(parse_card_file(card), strict=True)
    assert any(i.code == "capability-mismatch" for i in report.errors)


def test_locked_mode_requires_equality(tmp_path):
    card = _write_pair(
        tmp_path,
        card_caps="tip.compression.v1",
        adapter_caps="'tip.compression.v1', 'tip.cache.semantic.v1'",
    )
    report = validate_card(parse_card_file(card), mode="locked")
    assert any(i.code == "capability-mismatch" for i in report.errors)


def test_overclaiming_always_blocked_in_dev(tmp_path):
    """Card declares a capability NOT in the adapter frozenset → error (§H)."""
    card = _write_pair(
        tmp_path,
        card_caps="tip.compression.v1, tip.pak.recall",
        adapter_caps="'tip.compression.v1'",
    )
    report = validate_card(parse_card_file(card), mode="dev")
    assert any(i.code == "overclaimed-capability" for i in report.errors)


def test_overclaiming_always_blocked_in_strict(tmp_path):
    card = _write_pair(
        tmp_path,
        card_caps="tip.compression.v1, tip.pak.recall",
        adapter_caps="'tip.compression.v1'",
    )
    report = validate_card(parse_card_file(card), strict=True)
    assert any(i.code == "overclaimed-capability" for i in report.errors)


def test_missing_adapter_is_warning_in_dev_error_in_strict(tmp_path):
    base = tmp_path / "integrations" / "acme-llm"
    base.mkdir(parents=True)
    card = base / "acme-llm.tip.md"
    card.write_text(CARD_TEMPLATE.format(caps=""), encoding="utf-8")

    dev = validate_card(parse_card_file(card), mode="dev")
    assert dev.ok
    assert any(i.code == "adapter-missing" for i in dev.warnings)

    strict = validate_card(parse_card_file(card), strict=True)
    assert any(i.code == "adapter-missing" for i in strict.errors)


# ---------------------------------------------------------------------------
# Static adapter capability extraction (never imports)
# ---------------------------------------------------------------------------


def test_adapter_capabilities_read_without_execution(tmp_path):
    """The adapter raises at import time — AST extraction must still work."""
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        "raise RuntimeError('imported!')\n"
        "capabilities = frozenset({'tip.compression.v1'})\n",
        encoding="utf-8",
    )
    caps = extract_adapter_capabilities(adapter)
    assert caps == frozenset({"tip.compression.v1"})


def test_class_level_and_annotated_declarations_extracted(tmp_path):
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        "class AcmeLlmCredentialProvider:\n"
        "    capabilities: frozenset[str] = frozenset({'tip.cache.semantic.v1'})\n",
        encoding="utf-8",
    )
    caps = extract_adapter_capabilities(adapter)
    assert caps == frozenset({"tip.cache.semantic.v1"})


def test_unreadable_capability_declaration_returns_none(tmp_path):
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        "capabilities = compute_capabilities()\n", encoding="utf-8"
    )
    assert extract_adapter_capabilities(adapter) is None
