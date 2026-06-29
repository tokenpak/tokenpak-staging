# SPDX-License-Identifier: Apache-2.0
"""Hermes adapter stub.

Hermes is a routing target for which this module ships the *contract surface*
only. It declares the TIP capability contract an eventual Hermes adapter must
satisfy, but it cannot route real traffic:

* ``detect`` always returns ``False`` so the stub never auto-matches a live
  request — it is never selected by :class:`AdapterRegistry`;
* ``normalize`` / ``denormalize`` / ``get_default_upstream`` fail loud.

It is intentionally not added to ``build_default_registry``. Enabling live
routing is a separate activation step; see ``docs/hermes-adapter-spec.md``.
"""

from __future__ import annotations

from typing import Mapping, Optional

from tokenpak.tip.capabilities import (
    TIP_ROUTE_CLASS_V1,
    TIP_TELEMETRY_ATTRIBUTION_V1,
)

from .base import FormatAdapter
from .canonical import CanonicalRequest

_STUB_MESSAGE = (
    "Hermes adapter is a contract stub: live routing is not enabled. Implement "
    "and register a concrete Hermes adapter before routing traffic "
    "(see docs/hermes-adapter-spec.md)."
)


class HermesAdapter(FormatAdapter):
    """Contract-only Hermes adapter: declares its TIP contract, routes nothing."""

    source_format = "hermes"
    tip_min_version = "TIP-1.0"
    tip_max_version = "TIP-1.x"
    capabilities = frozenset({TIP_ROUTE_CLASS_V1, TIP_TELEMETRY_ATTRIBUTION_V1})

    def detect(self, path: str, headers: Mapping[str, str], body: Optional[bytes]) -> bool:
        # Never auto-match: a stub must not silently capture live traffic.
        return False

    def normalize(self, body: bytes) -> CanonicalRequest:
        raise NotImplementedError(_STUB_MESSAGE)

    def denormalize(self, canonical: CanonicalRequest) -> bytes:
        raise NotImplementedError(_STUB_MESSAGE)

    def get_default_upstream(self) -> str:
        raise NotImplementedError(_STUB_MESSAGE)
