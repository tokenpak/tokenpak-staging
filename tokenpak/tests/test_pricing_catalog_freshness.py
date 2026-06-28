# SPDX-License-Identifier: Apache-2.0
"""Current pricing catalog freshness guards."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from tokenpak.models import get_rates
from tokenpak.telemetry.cost import SEED_PRICING
from tokenpak.telemetry.pricing import _STALENESS_DAYS, PricingCatalog

ROOT = Path(__file__).resolve().parents[2]
SEED_CATALOG = ROOT / "tokenpak" / "models" / "data" / "seed_catalog.json"


def _seed_pricing(model: str) -> dict:
    for row in SEED_PRICING:
        if row["model"] == model:
            return row
    raise AssertionError(f"missing seed pricing row for {model}")


def test_seed_catalog_is_inside_ci_staleness_window():
    data = json.loads(SEED_CATALOG.read_text(encoding="utf-8"))
    updated = date.fromisoformat(data["_meta"]["updated"])
    assert (date.today() - updated).days <= _STALENESS_DAYS


@pytest.mark.parametrize(
    ("model", "input_rate", "output_rate", "cache_read"),
    [
        ("claude-fable-5", 10.0, 50.0, 1.0),
        ("claude-opus-4-8", 5.0, 25.0, 0.5),
        ("claude-opus-4-7", 5.0, 25.0, 0.5),
        ("claude-opus-4-6", 5.0, 25.0, 0.5),
        ("claude-opus-4-5", 5.0, 25.0, 0.5),
    ],
)
def test_current_anthropic_rates_match_catalog_surfaces(
    model: str,
    input_rate: float,
    output_rate: float,
    cache_read: float,
):
    registry_rates = get_rates(model)
    assert registry_rates["input"] == input_rate
    assert registry_rates["output"] == output_rate
    assert registry_rates["cached"] == cache_read

    catalog = PricingCatalog.load()
    catalog_rates = catalog.get_model(model)
    assert catalog_rates is not None
    assert catalog_rates.input_per_token * 1_000_000 == pytest.approx(input_rate)
    assert catalog_rates.output_per_token * 1_000_000 == pytest.approx(output_rate)
    assert catalog_rates.cache_read_per_token * 1_000_000 == pytest.approx(cache_read)

    seed_row = _seed_pricing(model)
    assert seed_row["input_rate"] == input_rate
    assert seed_row["output_rate"] == output_rate
