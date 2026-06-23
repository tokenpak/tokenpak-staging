"""Adapter registry with priority-based format detection."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Mapping, Optional, Tuple

from tokenpak.tip.adapter_contract import (
    ASSERTED_TIP_VERSION,
    AdapterCompatibilityError,
    validate_adapter_compatibility,
)

from .base import FormatAdapter

_log = logging.getLogger(__name__)


@dataclass
class _RegisteredAdapter:
    adapter: FormatAdapter
    priority: int


@dataclass
class AdapterSelfTestResult:
    """Outcome of the proxy-startup adapter TIP-compatibility self-test.

    ``passed`` holds the ``source_format`` of every adapter that validated and
    stays active; ``gated_out`` holds ``(source_format, public_safe_reason)``
    for every adapter removed from the active registry.
    """

    passed: List[str] = field(default_factory=list)
    gated_out: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True iff no adapter was gated out."""
        return not self.gated_out


class AdapterRegistry:
    """Registry for provider format adapters."""

    def __init__(self) -> None:
        self._items: List[_RegisteredAdapter] = []

    def register(self, adapter: FormatAdapter, priority: int = 100) -> None:
        self._items.append(_RegisteredAdapter(adapter=adapter, priority=priority))
        self._items.sort(key=lambda item: item.priority, reverse=True)

    def run_startup_self_test(
        self, asserted_tip_version: str = ASSERTED_TIP_VERSION
    ) -> AdapterSelfTestResult:
        """Validate every registered adapter against the asserted TIP version.

        Compatible adapters stay in the active registry; incompatible adapters
        are gated out (removed) with a logged warning so they can never serve
        live traffic. Returns the pass / gate-out summary. Callers map
        ``gated_out`` entries to ``tip_version_mismatch`` telemetry and, when
        nothing was gated out, emit ``tip_version_check.passed``.

        This is the load-time fail-loud gate: it runs the contract self-test
        before any request is routed. It does not enable routing for any
        adapter — registration and wiring remain the caller's responsibility.
        """
        result = AdapterSelfTestResult()
        kept: List[_RegisteredAdapter] = []
        for item in self._items:
            fmt = item.adapter.source_format
            try:
                validate_adapter_compatibility(
                    item.adapter.capability_contract(), asserted_tip_version
                )
            except AdapterCompatibilityError as exc:
                result.gated_out.append((fmt, str(exc)))
                _log.warning("tip_version_mismatch: gating out adapter %r — %s", fmt, exc)
                continue
            kept.append(item)
            result.passed.append(fmt)
        self._items = kept
        if result.ok:
            _log.info(
                "tip_version_check.passed: %d adapter(s) validated against %s",
                len(result.passed),
                asserted_tip_version,
            )
        return result

    def detect(
        self,
        path: str,
        headers: Mapping[str, str],
        body: Optional[bytes] = None,
    ) -> FormatAdapter:
        for item in self._items:
            if item.adapter.detect(path, headers, body):
                return item.adapter
        raise RuntimeError("No adapter matched request; ensure passthrough adapter is registered")

    def list_formats(self) -> List[str]:
        return [item.adapter.source_format for item in self._items]

    def adapters(self) -> List[FormatAdapter]:
        return [item.adapter for item in self._items]
