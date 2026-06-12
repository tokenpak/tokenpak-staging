# SPDX-License-Identifier: Apache-2.0
"""Compile tests — card → canonical manifest (Std 54 §C/§D, invariants 2–4).

The two packet-required compile fixtures:
* ``.pak.md`` compiles to a ``tokenpak.tip.Pak`` payload (proved by
  round-tripping the wire dict through ``Pak.from_dict``).
* ``.tip.md tip_kind: provider_adapter`` compiles to a provider adapter
  manifest with the Std 23 §2.2 class-name derivation.

Plus the invariant-2 guard: raw Markdown is never executed, and invalid
cards never produce manifests (invariant 4).
"""

from __future__ import annotations

import json

import pytest

from tokenpak.cards.compile import compile_card, derive_provider_class_name, write_compiled
from tokenpak.cards.model import (
    CAPABILITIES_SCHEMA,
    PAK_TARGET_SCHEMA,
    TARGET_CONTRACT_PAK,
    TARGET_CONTRACT_PROVIDER_ADAPTER,
    CardError,
)
from tokenpak.cards.parser import parse_card_file, parse_card_text

PAK_CARD = """---
card_kind: pak
name: release-notes-context
target_contract: tokenpak.tip.Pak
source_format: tokenpak-card-md-1
card_status: local
tokenpak_min_version: "1.5.0"
pak_subtype: recall
pak_status: accepted
authority: user_approved
confidence: high
retention: source_lifetime
privacy: local_only
capabilities: [tip.pak.recall]
category_tags: [release_notes]
advisory_risk_flags: [may_be_stale]
---

# Release notes context

Summarized release-notes knowledge for recall.
"""

TIP_CARD = """---
card_kind: tip
tip_kind: provider_adapter
name: acme-llm
target_contract: tokenpak.services.routing_service.CredentialProvider
source_format: tokenpak-card-md-1
card_status: local
tokenpak_min_version: "1.5.0"
capabilities: []
---

# acme-llm provider adapter
"""


# ---------------------------------------------------------------------------
# .pak.md → tokenpak.tip.Pak
# ---------------------------------------------------------------------------


def test_pak_card_compiles_to_canonical_pak():
    manifest = compile_card(parse_card_text(PAK_CARD))
    assert manifest["card_kind"] == "pak"
    assert manifest["target_contract"] == TARGET_CONTRACT_PAK
    assert manifest["target_schema"] == PAK_TARGET_SCHEMA

    # The payload must be parseable by the canonical frozen contract.
    from tokenpak.tip.pak import Pak, PakSubtype

    pak = Pak.from_dict(manifest["pak"])
    assert pak.pak_type is PakSubtype.RECALL
    assert pak.title == "Release notes context"
    assert "release-notes knowledge" in pak.summary
    assert pak.pak_id.startswith("pak:")
    assert pak.source.source_hash == manifest["source_sha256"]
    assert pak.privacy.class_.value == "local_only"
    assert pak.retention.ttl.value == "source_lifetime"


def test_pak_title_falls_back_to_name_without_h1():
    text = PAK_CARD.replace("# Release notes context\n", "")
    manifest = compile_card(parse_card_text(text))
    assert manifest["pak"]["title"] == "release-notes-context"


def test_pak_retention_defaults_from_contract_when_absent():
    text = PAK_CARD.replace("retention: source_lifetime\n", "")
    manifest = compile_card(parse_card_text(text))
    # default_retention_for(RECALL) == session (Std 32 §8)
    assert manifest["pak"]["retention"]["ttl"] == "session"


def test_capability_bearing_card_stamps_capabilities_schema():
    manifest = compile_card(parse_card_text(PAK_CARD))
    assert manifest["capabilities"] == ["tip.pak.recall"]
    assert manifest["capabilities_schema"] == CAPABILITIES_SCHEMA


def test_non_capability_card_does_not_stamp_capabilities_schema():
    manifest = compile_card(parse_card_text(TIP_CARD))
    # Only capability-bearing cards stamp tip-capabilities (Std 54 §C).
    assert "capabilities_schema" not in manifest


def test_authoring_metadata_kept_out_of_canonical_payload():
    manifest = compile_card(parse_card_text(PAK_CARD))
    assert manifest["authoring_metadata"]["category_tags"] == ["release_notes"]
    assert manifest["authoring_metadata"]["advisory_risk_flags"] == ["may_be_stale"]
    # No canonical Pak field exists for these — they must not leak into
    # the contract payload (Std 54 §D).
    assert "category_tags" not in manifest["pak"]
    assert "advisory_risk_flags" not in manifest["pak"]
    assert "risk_flags" not in manifest["pak"]


# ---------------------------------------------------------------------------
# .tip.md provider_adapter → adapter manifest
# ---------------------------------------------------------------------------


def test_tip_card_compiles_to_adapter_manifest():
    manifest = compile_card(parse_card_text(TIP_CARD))
    assert manifest["card_kind"] == "tip"
    assert manifest["target_contract"] == TARGET_CONTRACT_PROVIDER_ADAPTER
    adapter = manifest["adapter"]
    assert adapter["name"] == "acme-llm"
    assert adapter["tip_kind"] == "provider_adapter"
    assert adapter["provider_class"] == "AcmeLlmCredentialProvider"


@pytest.mark.parametrize(
    "slug,expected",
    [
        ("acme-llm", "AcmeLlmCredentialProvider"),
        ("mistral", "MistralCredentialProvider"),
        ("azure_openai", "AzureOpenaiCredentialProvider"),
    ],
)
def test_provider_class_name_derivation(slug, expected):
    assert derive_provider_class_name(slug) == expected


def test_tip_manifest_includes_adapter_capabilities_when_present(tmp_path):
    base = tmp_path / "integrations" / "acme-llm"
    base.mkdir(parents=True)
    card_path = base / "acme-llm.tip.md"
    card_path.write_text(
        TIP_CARD.replace("capabilities: []", "capabilities: [tip.compression.v1]"),
        encoding="utf-8",
    )
    (base / "adapter.py").write_text(
        "capabilities = frozenset({'tip.compression.v1'})\n", encoding="utf-8"
    )
    manifest = compile_card(parse_card_file(card_path))
    assert manifest["adapter"]["adapter_capabilities"] == ["tip.compression.v1"]
    assert manifest["adapter"]["capabilities"] == ["tip.compression.v1"]


# ---------------------------------------------------------------------------
# Invariants 2/4 — never execute, never emit from invalid cards
# ---------------------------------------------------------------------------


def test_raw_markdown_is_never_executed(tmp_path):
    """A card body full of executable-looking content compiles inertly."""
    marker = tmp_path / "executed"
    body = (
        "\n# Hostile body\n\n"
        "```python\n"
        f"import os\nos.system('touch {marker}')\nraise SystemExit(99)\n"
        "```\n"
        f"<script>fetch('http://localhost/x')</script>\n"
        f"$(touch {marker})\n"
    )
    text = PAK_CARD.split("# Release notes context")[0] + body
    manifest = compile_card(parse_card_text(text))
    assert not marker.exists()
    assert manifest["pak"]["title"] == "Hostile body"
    # The hostile text is carried as inert data only.
    assert "os.system" in manifest["pak"]["summary"]


def test_invalid_card_never_produces_a_manifest():
    text = PAK_CARD.replace("privacy: local_only", "privacy: team_local")
    with pytest.raises(CardError, match="failed validation"):
        compile_card(parse_card_text(text))


def test_write_compiled_emits_json_file(tmp_path):
    manifest = compile_card(parse_card_text(PAK_CARD))
    out = write_compiled(manifest, tmp_path / "compiled")
    assert out.name == "release-notes-context.json"
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert on_disk["pak"]["pak_id"] == manifest["pak"]["pak_id"]
