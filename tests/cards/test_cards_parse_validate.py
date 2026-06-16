# SPDX-License-Identifier: Apache-2.0
"""Parse + validate tests for the Cards authoring layer (Std 54 §A/§C/§E).

Covers the Phase-1 frontmatter contract, the never-execute-raw-Markdown
invariant (2), Phase-2 kind rejection, canonical enum validation, and
the capability-vocabulary rule (invariant 8 — no inline invention).
"""

from __future__ import annotations

import pytest

from tokenpak.cards.model import CardError
from tokenpak.cards.parser import parse_card_text, scan_env_references, split_frontmatter
from tokenpak.cards.validate import validate_card

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_PAK_CARD = """---
card_kind: pak
name: sample-knowledge
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

category_tags: [sample_context]
advisory_risk_flags: [may_contain_internal_information]
---

# Sample knowledge

Body text for the sample knowledge Pak.
"""

VALID_TIP_CARD = """---
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

Uses ${ACME_API_KEY} from the environment.
"""


def _pak_card(**overrides: object) -> str:
    """Build a pak card with frontmatter overrides (None deletes a key)."""
    import yaml

    fm_text, body = split_frontmatter(VALID_PAK_CARD)
    fm = yaml.safe_load(fm_text)
    for k, v in overrides.items():
        if v is None:
            fm.pop(k, None)
        else:
            fm[k] = v
    return "---\n" + yaml.safe_dump(fm) + "---\n" + body


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_valid_pak_card():
    card = parse_card_text(VALID_PAK_CARD)
    assert card.card_kind == "pak"
    assert card.name == "sample-knowledge"
    assert card.body_title() == "Sample knowledge"
    assert len(card.source_sha256) == 64


def test_parse_requires_frontmatter():
    with pytest.raises(CardError, match="no YAML frontmatter"):
        parse_card_text("# just markdown\n")


def test_parse_rejects_unterminated_frontmatter():
    with pytest.raises(CardError, match="unterminated"):
        parse_card_text("---\ncard_kind: pak\n# body without closing delim\n")


def test_parse_rejects_non_mapping_frontmatter():
    with pytest.raises(CardError, match="mapping"):
        parse_card_text("---\n- a\n- b\n---\nbody\n")


def test_python_yaml_tags_never_construct_objects(tmp_path):
    """Invariant 2: frontmatter is safe_load only — !!python tags fail."""
    marker = tmp_path / "pwned"
    evil = (
        "---\n"
        f'card_kind: !!python/object/apply:os.system ["touch {marker}"]\n'
        "---\nbody\n"
    )
    with pytest.raises(CardError, match="not valid YAML"):
        parse_card_text(evil)
    assert not marker.exists()


def test_env_reference_scan_is_static():
    card = parse_card_text(VALID_TIP_CARD)
    assert scan_env_references(card) == ["ACME_API_KEY"]


# ---------------------------------------------------------------------------
# Validator — common contract
# ---------------------------------------------------------------------------


def test_valid_pak_card_passes():
    report = validate_card(parse_card_text(VALID_PAK_CARD))
    assert report.ok, [i.message for i in report.issues]


def test_valid_tip_card_passes_dev_mode():
    report = validate_card(parse_card_text(VALID_TIP_CARD))
    # No adapter.py next to an in-memory card → dev-mode warning only.
    assert report.ok
    assert any(i.code == "adapter-missing" for i in report.warnings)


@pytest.mark.parametrize(
    "field", ["card_kind", "name", "target_contract", "source_format", "card_status"]
)
def test_missing_required_common_field_is_error(field):
    report = validate_card(parse_card_text(_pak_card(**{field: None})))
    assert not report.ok
    assert any(i.code == "missing-field" for i in report.errors)


def test_bad_source_format_is_error():
    report = validate_card(parse_card_text(_pak_card(source_format="markdown-v2")))
    assert any(i.code == "bad-source-format" for i in report.errors)


def test_bad_card_status_is_error():
    report = validate_card(parse_card_text(_pak_card(card_status="published")))
    assert any(i.code == "bad-card-status" for i in report.errors)


def test_name_with_path_separator_is_error():
    report = validate_card(parse_card_text(_pak_card(name="../escape")))
    assert any(i.code == "bad-name" for i in report.errors)


def test_unknown_field_is_warning_not_error():
    report = validate_card(parse_card_text(_pak_card(favorite_color="green")))
    assert report.ok
    assert any(i.code == "unknown-field" for i in report.warnings)


def test_risk_flags_rename_hint():
    report = validate_card(parse_card_text(_pak_card(risk_flags=["x"])))
    assert any(i.code == "renamed-field" for i in report.warnings)


# ---------------------------------------------------------------------------
# Validator — Phase-2 rejection (Std 54 §B)
# ---------------------------------------------------------------------------


def test_worker_card_kind_rejected_as_phase2():
    report = validate_card(parse_card_text(_pak_card(card_kind="worker")))
    assert any(i.code == "phase2-kind" for i in report.errors)


@pytest.mark.parametrize("tip_kind", ["context_connector", "mcp_bridge"])
def test_phase2_tip_kinds_rejected(tip_kind):
    text = VALID_TIP_CARD.replace("tip_kind: provider_adapter", f"tip_kind: {tip_kind}")
    report = validate_card(parse_card_text(text))
    assert any(i.code == "phase2-kind" for i in report.errors)


def test_project_is_not_a_card_kind():
    report = validate_card(parse_card_text(_pak_card(card_kind="project")))
    assert any(i.code == "not-a-card" for i in report.errors)


# ---------------------------------------------------------------------------
# Validator — pak canonical enums (Std 54 §D)
# ---------------------------------------------------------------------------


def test_wrong_target_contract_for_pak_is_error():
    report = validate_card(parse_card_text(_pak_card(target_contract="tokenpak.tip.Other")))
    assert any(i.code == "bad-target-contract" for i in report.errors)


def test_wrong_target_contract_for_tip_is_error():
    text = VALID_TIP_CARD.replace(
        "target_contract: tokenpak.services.routing_service.CredentialProvider",
        "target_contract: tokenpak.tip.Pak",
    )
    report = validate_card(parse_card_text(text))
    assert any(i.code == "bad-target-contract" for i in report.errors)


@pytest.mark.parametrize(
    "field,value",
    [
        ("pak_subtype", "knowledge"),
        ("pak_status", "live"),
        ("authority", "self_declared"),
        ("confidence", "absolute"),
        ("retention", "forever"),
        ("privacy", "team_local"),  # v1 admits local_only only (Std 32 §1.2)
    ],
)
def test_non_canonical_pak_enum_is_error(field, value):
    report = validate_card(parse_card_text(_pak_card(**{field: value})))
    assert any(i.code == "bad-enum" for i in report.errors), field


def test_deprecated_subtype_alias_warns_but_validates():
    report = validate_card(parse_card_text(_pak_card(pak_subtype="memory")))
    assert report.ok
    assert any(i.code == "deprecated-alias" for i in report.warnings)


def test_retention_optional_defaults_via_contract():
    report = validate_card(parse_card_text(_pak_card(retention=None)))
    assert report.ok


# ---------------------------------------------------------------------------
# Validator — capability vocabulary (Std 54 §E / invariant 8)
# ---------------------------------------------------------------------------


def test_canonical_capability_accepted():
    report = validate_card(
        parse_card_text(_pak_card(capabilities=["tip.pak.recall"]))
    )
    assert report.ok


def test_invented_tip_capability_blocked():
    report = validate_card(
        parse_card_text(_pak_card(capabilities=["tip.cards.magic.v9"]))
    )
    assert any(i.code == "invented-capability" for i in report.errors)


def test_ext_capability_is_metadata_warning():
    report = validate_card(
        parse_card_text(_pak_card(capabilities=["ext.acme.priority-lane"]))
    )
    assert report.ok
    assert any(i.code == "ext-capability" for i in report.warnings)


def test_malformed_capability_blocked():
    report = validate_card(
        parse_card_text(_pak_card(capabilities=["compression-v1"]))
    )
    assert any(i.code == "bad-capabilities" for i in report.errors)
