# SPDX-License-Identifier: Apache-2.0
"""TIP adapter capability contract.

Protocol-layer contract for adapter compatibility. An adapter declares the
TIP version range it supports (``tip_min_version`` / ``tip_max_version``) and
the optimization capabilities it publishes; the proxy validates each adapter
against the asserted runtime TIP version at load / startup time and gates out
anything incompatible *before* it can serve traffic.

This module belongs to the TIP/Protocol layer: it imports only from
``tokenpak.tip.capabilities`` and the standard library — never from proxy or
adapter implementation modules (see the ``tokenpak.tip`` package docstring).

Version model
-------------
TIP versions use the label form ``TIP-MAJOR.MINOR`` and carry no patch
segment. ``TIP-MAJOR.x`` is the minor-wildcard shorthand meaning "any minor
within this major"; it is normalized to an internal upper bound for
comparison while public manifests keep the shorthand. Ranges declared by an
adapter are inclusive on both ends.

Author usage — declare a contract::

    from tokenpak.tip.adapter_contract import AdapterCapabilityContract
    from tokenpak.tip.capabilities import TIP_COMPRESSION_V1

    contract = AdapterCapabilityContract(
        adapter_name="my-provider",
        tip_min_version="TIP-1.0",
        tip_max_version="TIP-1.x",
        capabilities=frozenset({TIP_COMPRESSION_V1}),
    )
    contract.validate()  # raises AdapterCompatibilityError if incompatible
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Tuple

from tokenpak.tip.capabilities import ALL_OPTIMIZATION_CAPABILITIES

# The TIP version the running proxy asserts. Mirrors the core protocol
# SUPPORTED_VERSIONS ({"1.0"}); kept as the canonical TIP-MAJOR.MINOR label.
ASSERTED_TIP_VERSION = "TIP-1.0"

# Default adapter manifest version bounds: accept any minor within TIP-1.
TIP_VERSION_DEFAULT_MIN = "TIP-1.0"
TIP_VERSION_DEFAULT_MAX = "TIP-1.x"

# Vendor/external capability namespace (see capabilities.py label format note).
_EXT_CAPABILITY_PREFIX = "ext."

# Internal ceiling for the ".x" minor wildcard when used as an upper bound.
_MINOR_WILDCARD_CEILING = 1_000_000


class AdapterCompatibilityError(Exception):
    """Raised when an adapter's declared TIP contract is incompatible.

    Messages are public-safe by construction: they name the adapter, the TIP
    version labels, and the offending capability labels only — never
    filesystem paths, hostnames, or other host/internal detail. The contract
    fails loud; an invalid value is never silently stripped.
    """


@dataclass(frozen=True)
class TipVersion:
    """Parsed ``TIP-MAJOR.MINOR`` label, with ``TIP-MAJOR.x`` wildcard support.

    ``minor`` is ``None`` for the ``.x`` wildcard ("any minor within this
    major"). Versions carry no patch segment.
    """

    major: int
    minor: int | None  # None == ".x" wildcard

    @classmethod
    def parse(cls, label: str) -> "TipVersion":
        """Parse a TIP version label; raise AdapterCompatibilityError if malformed."""
        if not isinstance(label, str):
            raise AdapterCompatibilityError(
                f"TIP version must be a string, got {type(label).__name__}"
            )
        raw = label.strip()
        if not raw.startswith("TIP-"):
            raise AdapterCompatibilityError(
                f"invalid TIP version {label!r}: must start with 'TIP-' "
                "(expected 'TIP-MAJOR.MINOR' or 'TIP-MAJOR.x')"
            )
        body = raw[len("TIP-"):]
        parts = body.split(".")
        if len(parts) != 2:
            raise AdapterCompatibilityError(
                f"invalid TIP version {label!r}: expected 'TIP-MAJOR.MINOR' "
                "or 'TIP-MAJOR.x'"
            )
        major_s, minor_s = parts
        try:
            major = int(major_s)
        except ValueError:
            raise AdapterCompatibilityError(
                f"invalid TIP version {label!r}: major segment must be an integer"
            ) from None
        if major < 0:
            raise AdapterCompatibilityError(
                f"invalid TIP version {label!r}: major segment must be >= 0"
            )
        if minor_s in ("x", "X"):
            minor: int | None = None
        else:
            try:
                minor = int(minor_s)
            except ValueError:
                raise AdapterCompatibilityError(
                    f"invalid TIP version {label!r}: minor segment must be an "
                    "integer or 'x'"
                ) from None
            if minor < 0:
                raise AdapterCompatibilityError(
                    f"invalid TIP version {label!r}: minor segment must be >= 0"
                )
        return cls(major=major, minor=minor)

    def floor_key(self) -> Tuple[int, int]:
        """Comparison key when this version is a lower bound (``.x`` -> minor 0)."""
        return (self.major, 0 if self.minor is None else self.minor)

    def ceiling_key(self) -> Tuple[int, int]:
        """Comparison key when this version is an upper bound (``.x`` -> wildcard ceiling)."""
        return (self.major, _MINOR_WILDCARD_CEILING if self.minor is None else self.minor)

    def point_key(self) -> Tuple[int, int]:
        """Comparison key for a concrete asserted version (``.x`` -> minor 0)."""
        return (self.major, 0 if self.minor is None else self.minor)


def is_known_capability(label: str) -> bool:
    """True if ``label`` is a recognized TIP capability or a vendor ``ext.*`` label."""
    if not isinstance(label, str) or not label:
        return False
    if label in ALL_OPTIMIZATION_CAPABILITIES:
        return True
    return label.startswith(_EXT_CAPABILITY_PREFIX) and len(label) > len(_EXT_CAPABILITY_PREFIX)


@dataclass(frozen=True)
class AdapterCapabilityContract:
    """The TIP-version + capability contract an adapter declares.

    ``tip_min_version`` / ``tip_max_version`` are inclusive on both ends.
    ``capabilities`` are ``tip.*`` labels from the optimization vocabulary
    (``tokenpak.tip.capabilities``) or vendor ``ext.<vendor>.<feature>`` labels.
    """

    adapter_name: str
    tip_min_version: str = TIP_VERSION_DEFAULT_MIN
    tip_max_version: str = TIP_VERSION_DEFAULT_MAX
    capabilities: FrozenSet[str] = field(default_factory=frozenset)

    def validate(self, asserted_tip_version: str = ASSERTED_TIP_VERSION) -> None:
        """Run the compatibility self-test; raise AdapterCompatibilityError on mismatch."""
        validate_adapter_compatibility(self, asserted_tip_version)


def validate_adapter_compatibility(
    contract: AdapterCapabilityContract,
    asserted_tip_version: str = ASSERTED_TIP_VERSION,
) -> None:
    """Validate one adapter contract against the asserted runtime TIP version.

    Self-test, fail-loud primitive:

    1. Parse ``asserted_tip_version``, ``tip_min_version``, ``tip_max_version``.
    2. Require ``tip_min_version <= asserted_tip_version <= tip_max_version``
       (inclusive) and every declared capability to be a known label.

    Raises :class:`AdapterCompatibilityError` (public-safe message) on any
    failure. Callers that want the "gate out + telemetry" behavior wrap this in
    a ``try/except`` (see ``AdapterRegistry.run_startup_self_test``).
    """
    name = contract.adapter_name or "<unnamed adapter>"
    asserted = TipVersion.parse(asserted_tip_version)
    minimum = TipVersion.parse(contract.tip_min_version)
    maximum = TipVersion.parse(contract.tip_max_version)

    point = asserted.point_key()
    if point < minimum.floor_key():
        raise AdapterCompatibilityError(
            f"adapter {name!r} requires TIP >= {contract.tip_min_version} but "
            f"the proxy asserts {asserted_tip_version}"
        )
    if point > maximum.ceiling_key():
        raise AdapterCompatibilityError(
            f"adapter {name!r} supports TIP <= {contract.tip_max_version} but "
            f"the proxy asserts {asserted_tip_version}"
        )

    unknown = sorted(c for c in contract.capabilities if not is_known_capability(c))
    if unknown:
        raise AdapterCompatibilityError(
            f"adapter {name!r} declares unknown TIP capability label(s): "
            f"{', '.join(unknown)}"
        )


__all__ = [
    "ASSERTED_TIP_VERSION",
    "TIP_VERSION_DEFAULT_MIN",
    "TIP_VERSION_DEFAULT_MAX",
    "AdapterCompatibilityError",
    "TipVersion",
    "AdapterCapabilityContract",
    "is_known_capability",
    "validate_adapter_compatibility",
]
