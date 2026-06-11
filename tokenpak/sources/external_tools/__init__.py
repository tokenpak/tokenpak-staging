# SPDX-License-Identifier: Apache-2.0
"""Non-routing external-tool TIP source adapters.

This package defines the *observation-only* source-adapter category:
adapters that read activity surfaces a third-party developer tool already
writes (session transcripts, on-disk state) and project what they see into
TIP-shaped observed records.

Category contract (binding for every adapter in this package):

1. **Non-routing.** Adapters never forward model traffic, never read or
   inject credentials, and never participate in proxy request handling.
   They are not provider adapters and must not be registered as such.
2. **Read-only.** Adapters observe; they must never modify the observed
   tool's invocation, configuration, credentials, state, or output.
3. **Honest provenance.** Every record is labeled as *TokenPak-observed* —
   derived by TokenPak from the tool's surface. A record must never claim
   the tool itself emitted TIP. Attribution defaults to ``client`` when
   the true source is unknown.
4. **Extension labels only.** Capability labels live under
   ``ext.<tool>.<feature>``. The ``tip.*`` namespace is reserved for
   protocol-native capabilities and is rejected at validation time.
5. **No unbacked savings claims.** Observed records carry raw observed
   counts only; derived savings/efficiency figures are out of scope.
6. **Runtime-discovered.** Adapters are found by scanning this package
   and by explicit :func:`register` calls — there is no central enum to
   edit. Adding a tool must require zero changes to the base interface,
   the registry, or any sibling adapter.

This is deliberately a *sibling* of the content-ingestion
``tokenpak.sources.base_source.SourceAdapter`` interface, not a subclass:
content ingestion returns ``(content, Provenance)`` pairs destined for the
vault index, while this category returns lists of
:class:`ObservedToolRecord` destined for the TIP surface.
"""

from __future__ import annotations

from tokenpak.sources.external_tools.base import (
    EXT_LABEL_PATTERN,
    OBSERVATION_PROVENANCE,
    ExternalToolTIPSource,
    ObservedSession,
    ObservedToolRecord,
    RecordValidationError,
    extract_message_text,
    sanitize_label_segment,
)
from tokenpak.sources.external_tools.registry import (
    ENV_FLAG,
    all_sources,
    enabled_slugs,
    get,
    load_builtins,
    observe_sessions,
    register,
    reset_registry,
    unregister,
)

__all__ = [
    # base / category contract
    "ExternalToolTIPSource",
    "ObservedSession",
    "ObservedToolRecord",
    "RecordValidationError",
    "EXT_LABEL_PATTERN",
    "OBSERVATION_PROVENANCE",
    "extract_message_text",
    "sanitize_label_segment",
    # registry / runner
    "ENV_FLAG",
    "register",
    "unregister",
    "get",
    "all_sources",
    "enabled_slugs",
    "load_builtins",
    "observe_sessions",
    "reset_registry",
]
