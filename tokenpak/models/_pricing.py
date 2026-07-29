# SPDX-License-Identifier: Apache-2.0
"""Context-aware pricing primitives shared by registry and telemetry.

Scalar model rates remain the compatibility default.  A ``RateBand`` is a
complete replacement rate tuple selected only when a caller supplies a
``PricingContext``.  Complete tuples avoid ambiguous partial overlays when a
provider combines context-length, modality, and service-tier pricing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite


def _normalize_selector(values: object, field_name: str) -> tuple[str, ...]:
    if values is None:
        return ("*",)
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise ValueError(f"{field_name} must be a list of strings")
    if any(not isinstance(value, str) for value in values):
        raise ValueError(f"{field_name} must contain only strings")
    normalized = tuple(value.strip().lower() for value in values)
    if not normalized or any(not value for value in normalized):
        raise ValueError(f"{field_name} must contain non-empty strings")
    if "*" in normalized and len(normalized) != 1:
        raise ValueError(f"{field_name} wildcard must be the only value")
    return normalized


def _required_string(data: Mapping[str, object], key: str, label: str) -> str:
    try:
        value = data[key]
    except KeyError as exc:
        raise ValueError(f"rate band missing required field: {key}") from exc
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value


def _number(data: Mapping[str, object], key: str, label: str) -> float:
    try:
        value = data[key]
    except KeyError as exc:
        raise ValueError(f"rate band missing required field: {key}") from exc
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    return float(value)


def _optional_number(data: Mapping[str, object], key: str, label: str) -> float | None:
    if key not in data:
        return None
    return _number(data, key, label)


def _optional_integer(data: Mapping[str, object], key: str, default: int | None) -> int | None:
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


@dataclass(frozen=True)
class PricingContext:
    """Request facts that can select a non-scalar provider rate band."""

    input_tokens: int | None = None
    input_modality: str = "text"
    output_modality: str = "text"
    service_tier: str = "standard"

    def __post_init__(self) -> None:
        if self.input_tokens is not None:
            if isinstance(self.input_tokens, bool) or not isinstance(self.input_tokens, int):
                raise ValueError("input_tokens must be an integer")
            if self.input_tokens < 0:
                raise ValueError("input_tokens must be non-negative")
        for field_name in ("input_modality", "output_modality", "service_tier"):
            raw_value = getattr(self, field_name)
            if not isinstance(raw_value, str):
                raise ValueError(f"{field_name} must be a string")
            value = raw_value.strip().lower()
            if not value:
                raise ValueError(f"{field_name} must be non-empty")
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True)
class RateBand:
    """A complete rate tuple and the request context in which it applies."""

    band_id: str
    input_per_mtok: float
    output_per_mtok: float
    source: str
    source_url: str
    cache_read_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None
    min_input_tokens: int = 0
    max_input_tokens: int | None = None
    input_modalities: tuple[str, ...] = ("*",)
    output_modalities: tuple[str, ...] = ("*",)
    service_tiers: tuple[str, ...] = ("*",)

    def __post_init__(self) -> None:
        if not isinstance(self.band_id, str):
            raise ValueError("rate band id must be a string")
        band_id = self.band_id.strip()
        if not band_id:
            raise ValueError("rate band id must be non-empty")
        object.__setattr__(self, "band_id", band_id)

        for field_name in (
            "input_per_mtok",
            "output_per_mtok",
            "cache_read_per_mtok",
            "cache_write_per_mtok",
        ):
            value = getattr(self, field_name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{field_name} must be a number")
            if not isfinite(value) or value < 0:
                raise ValueError(f"{field_name} must be finite and non-negative")
            object.__setattr__(self, field_name, float(value))
        if isinstance(self.min_input_tokens, bool) or not isinstance(self.min_input_tokens, int):
            raise ValueError("min_input_tokens must be an integer")
        if self.min_input_tokens < 0:
            raise ValueError("min_input_tokens must be non-negative")
        if self.max_input_tokens is not None and (
            isinstance(self.max_input_tokens, bool) or not isinstance(self.max_input_tokens, int)
        ):
            raise ValueError("max_input_tokens must be an integer")
        if self.max_input_tokens is not None and self.max_input_tokens <= self.min_input_tokens:
            raise ValueError("max_input_tokens must be greater than min_input_tokens")

        for field_name in ("input_modalities", "output_modalities", "service_tiers"):
            object.__setattr__(
                self,
                field_name,
                _normalize_selector(getattr(self, field_name), field_name),
            )
        if not isinstance(self.source, str):
            raise ValueError("rate band source must be a string")
        source = self.source.strip().lower()
        if not source:
            raise ValueError("rate band source must be non-empty")
        object.__setattr__(self, "source", source)
        if not isinstance(self.source_url, str):
            raise ValueError("rate band source_url must be a string")
        source_url = self.source_url.strip()
        if not source_url:
            raise ValueError("rate band source_url must be non-empty")
        object.__setattr__(self, "source_url", source_url)

    @classmethod
    def from_mapping(cls, data: object) -> RateBand:
        """Parse one catalog ``pricing_bands`` entry."""
        if not isinstance(data, Mapping):
            raise ValueError("rate band must be an object")
        selectors_obj = data.get("selectors", {})
        rates_obj = data.get("rates", {})
        provenance_obj = data.get("provenance", {})
        if not isinstance(selectors_obj, Mapping):
            raise ValueError("rate band selectors must be an object")
        if not isinstance(rates_obj, Mapping):
            raise ValueError("rate band rates must be an object")
        if not isinstance(provenance_obj, Mapping):
            raise ValueError("rate band provenance must be an object")

        return cls(
            band_id=_required_string(data, "id", "rate band id"),
            input_per_mtok=_number(rates_obj, "input", "rate band input rate"),
            output_per_mtok=_number(rates_obj, "output", "rate band output rate"),
            source=_required_string(provenance_obj, "source", "rate band source"),
            source_url=_required_string(provenance_obj, "source_url", "rate band source_url"),
            cache_read_per_mtok=_optional_number(
                rates_obj, "cache_read", "rate band cache_read rate"
            ),
            cache_write_per_mtok=_optional_number(
                rates_obj, "cache_write", "rate band cache_write rate"
            ),
            min_input_tokens=_optional_integer(selectors_obj, "min_input_tokens", 0) or 0,
            max_input_tokens=_optional_integer(selectors_obj, "max_input_tokens", None),
            input_modalities=_normalize_selector(
                selectors_obj.get("input_modalities"), "input_modalities"
            ),
            output_modalities=_normalize_selector(
                selectors_obj.get("output_modalities"), "output_modalities"
            ),
            service_tiers=_normalize_selector(selectors_obj.get("service_tiers"), "service_tiers"),
        )

    def matches(self, context: PricingContext) -> bool:
        if context.input_tokens is None:
            if self.min_input_tokens or self.max_input_tokens is not None:
                return False
        else:
            if context.input_tokens < self.min_input_tokens:
                return False
            if self.max_input_tokens is not None and context.input_tokens >= self.max_input_tokens:
                return False
        return (
            _selector_matches(self.input_modalities, context.input_modality)
            and _selector_matches(self.output_modalities, context.output_modality)
            and _selector_matches(self.service_tiers, context.service_tier)
        )

    def is_more_specific_than(self, other: RateBand) -> bool:
        """Return whether this band's match set is a strict subset of ``other``."""
        comparisons = [
            _selector_subset(self.input_modalities, other.input_modalities),
            _selector_subset(self.output_modalities, other.output_modalities),
            _selector_subset(self.service_tiers, other.service_tiers),
            _range_subset(self, other),
        ]
        return all(subset for subset, _strict in comparisons) and any(
            strict for _subset, strict in comparisons
        )


def _selector_matches(selector: tuple[str, ...], value: str) -> bool:
    return selector == ("*",) or value in selector


def _selector_subset(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[bool, bool]:
    if right == ("*",):
        return (True, left != ("*",))
    if left == ("*",):
        return (False, False)
    left_values = set(left)
    right_values = set(right)
    return (left_values <= right_values, left_values < right_values)


def _range_subset(left: RateBand, right: RateBand) -> tuple[bool, bool]:
    lower_subset = left.min_input_tokens >= right.min_input_tokens
    upper_subset = right.max_input_tokens is None or (
        left.max_input_tokens is not None and left.max_input_tokens <= right.max_input_tokens
    )
    strict = left.min_input_tokens > right.min_input_tokens or (
        left.max_input_tokens is not None
        and (right.max_input_tokens is None or left.max_input_tokens < right.max_input_tokens)
    )
    return (lower_subset and upper_subset, strict)


def select_rate_band(bands: Sequence[RateBand], context: PricingContext | None) -> RateBand | None:
    """Select the uniquely most-specific matching band, rejecting overlaps."""
    if context is None:
        return None
    matches = [band for band in bands if band.matches(context)]
    if not matches:
        return None
    best = [
        band
        for band in matches
        if not any(other.is_more_specific_than(band) for other in matches if other is not band)
    ]
    if len(best) != 1:
        ids = ", ".join(sorted(band.band_id for band in best))
        raise ValueError(f"ambiguous pricing rate bands: {ids}")
    return best[0]
