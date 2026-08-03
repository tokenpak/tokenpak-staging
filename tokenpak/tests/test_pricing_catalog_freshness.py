# SPDX-License-Identifier: Apache-2.0
"""Current pricing catalog freshness and cross-surface guards."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from tokenpak.models import get_rates, get_registry
from tokenpak.prove.adapter import _get_model_rates, list_providers
from tokenpak.telemetry.cost import (
    CURRENT_EFFECTIVE_DATE,
    CURRENT_PRICING_VERSION,
    SEED_PRICING,
)
from tokenpak.telemetry.model_analytics import get_model_pricing
from tokenpak.telemetry.pricing import _STALENESS_DAYS, PricingCatalog

ROOT = Path(__file__).resolve().parents[2]
SEED_CATALOG = ROOT / "tokenpak" / "models" / "data" / "seed_catalog.json"
OFFICIAL_SOURCES = {
    "anthropic": "https://platform.claude.com/docs/en/about-claude/pricing",
    "openai": "https://developers.openai.com/api/docs/pricing",
    "google": "https://ai.google.dev/gemini-api/docs/pricing",
}


def _catalog_data() -> dict:
    return json.loads(SEED_CATALOG.read_text(encoding="utf-8"))


def _seed_pricing(model: str) -> dict:
    for row in SEED_PRICING:
        if row["model"] == model:
            return row
    raise AssertionError(f"missing telemetry seed pricing row for {model}")


def test_seed_catalog_metadata_is_current_and_sourced():
    data = _catalog_data()
    updated = date.fromisoformat(data["_meta"]["updated"])
    assert (date.today() - updated).days <= _STALENESS_DAYS
    assert data["_meta"]["sources"] == OFFICIAL_SOURCES
    assert data["_meta"]["pricing_version"] == CURRENT_PRICING_VERSION
    assert data["_meta"]["effective_date"] == CURRENT_EFFECTIVE_DATE


def test_sonnet_5_introductory_rate_has_not_expired():
    assert date.today() <= date(2026, 8, 31), (
        "Claude Sonnet 5 introductory pricing expired; refresh its standard rate before merging"
    )


@pytest.mark.parametrize(
    ("model", "input_rate", "output_rate", "cache_read"),
    [
        ("claude-fable-5", 10.0, 50.0, 1.0),
        ("claude-opus-5", 5.0, 25.0, 0.5),
        ("claude-opus-4-8", 5.0, 25.0, 0.5),
        ("claude-sonnet-5", 2.0, 10.0, 0.2),
        ("claude-haiku-4-5", 1.0, 5.0, 0.1),
        ("gpt-5.2", 1.75, 14.0, 0.175),
        ("gpt-5.3-codex", 1.75, 14.0, 0.175),
        ("gpt-4.1", 2.0, 8.0, 0.5),
        ("o3", 2.0, 8.0, 0.5),
        ("gemini-3.6-flash", 1.5, 7.5, 0.15),
        ("gemini-3.5-flash-lite", 0.3, 2.5, 0.03),
        ("gemini-2.5-flash-lite", 0.1, 0.4, 0.01),
    ],
)
def test_verified_rates_match_all_runtime_surfaces(
    model: str,
    input_rate: float,
    output_rate: float,
    cache_read: float,
):
    registry_rates = get_rates(model)
    assert registry_rates["input"] == pytest.approx(input_rate)
    assert registry_rates["output"] == pytest.approx(output_rate)
    assert registry_rates["cached"] == pytest.approx(cache_read)

    provider = get_registry().resolve(model).provider
    prove_rates = _get_model_rates(provider, model)
    assert prove_rates["input"] == pytest.approx(input_rate)
    assert prove_rates["output"] == pytest.approx(output_rate)
    assert prove_rates["cached"] == pytest.approx(cache_read)

    analytics_rates = get_model_pricing(model)
    assert analytics_rates["input"] == pytest.approx(input_rate)
    assert analytics_rates["output"] == pytest.approx(output_rate)

    catalog_rates = PricingCatalog.load().get_model(model)
    assert catalog_rates is not None
    assert catalog_rates.input_per_token * 1_000_000 == pytest.approx(input_rate)
    assert catalog_rates.output_per_token * 1_000_000 == pytest.approx(output_rate)
    assert catalog_rates.cache_read_per_token is not None
    assert catalog_rates.cache_read_per_token * 1_000_000 == pytest.approx(cache_read)

    seed_row = _seed_pricing(model)
    assert seed_row["input_rate"] == pytest.approx(input_rate)
    assert seed_row["output_rate"] == pytest.approx(output_rate)
    assert seed_row["cache_read_rate"] == pytest.approx(cache_read)


def test_unverified_or_retired_aliases_are_not_active_catalog_rows():
    models = _catalog_data()["models"]
    assert "gpt-5.3" not in models
    assert "gpt-5.3-codex-spark" not in models
    assert "gemini-2-flash" not in models
    assert "gemini-1.5-pro" not in models


def test_prove_provider_listing_uses_canonical_catalog_models():
    providers = {row["name"]: row for row in list_providers()}
    assert "claude-opus-4-8" in providers["anthropic"]["models"]
    assert "gpt-5.3-codex" in providers["openai"]["models"]
    assert "gemini-3.6-flash" in providers["google"]["models"]


def test_prove_preserves_explicit_custom_provider_rate_overrides(monkeypatch):
    from tokenpak.prove import adapter

    override = {"input": 9.0, "output": 19.0, "cached": 0.9}
    monkeypatch.setattr(
        adapter,
        "_user_providers",
        {"anthropic": {"models": {"claude-opus-4-8": override}}},
    )
    assert adapter._get_model_rates("anthropic", "claude-opus-4-8") == override


def test_prove_preserves_xai_compatibility_rates():
    assert _get_model_rates("xai", "grok-3") == {
        "input": 3.0,
        "output": 15.0,
        "cached": 0.30,
    }
