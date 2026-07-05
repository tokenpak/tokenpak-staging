# SPDX-License-Identifier: Apache-2.0
"""Model max-context-window lookup for spend-guard threshold derivation.

The spend guard's soft-block threshold derives dynamically from the selected
model's context window (default 80% of max). When a model's context window
is unknown, the caller falls back to the configured static fallback in
``SpendGuardConfig.block_tokens`` rather than silently assuming a large
window, and audits ``threshold_hit=block_tokens_fallback``.

The table itself lives in the ``tokenpak.models`` registry seed catalog
(``context_windows`` section of ``models/data/seed_catalog.json``) — the
single source of truth for model metadata. This module is a thin delegating
shim kept for the spend-guard import surface.
"""

from __future__ import annotations

from tokenpak.models import get_model_max_context as _registry_max_context
from tokenpak.models import get_registry


def get_model_max_context(model_id: str | None) -> int | None:
    """Resolve the max context window in tokens for a model id.

    Returns ``None`` when the model is unknown — the caller is responsible
    for falling back to the configured static threshold rather than silently
    assuming a default.
    """
    return _registry_max_context(model_id)


def known_models() -> list[str]:
    """Return the context-window catalog's model-id keys (sorted)."""
    return get_registry().known_context_window_models()
