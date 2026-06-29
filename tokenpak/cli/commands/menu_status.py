# SPDX-License-Identifier: Apache-2.0
"""Backward-compatible shim for the interactive-menu status source.

The cached, non-blocking, honest proxy-status probe that used to live here has
moved to the render-agnostic provider package ``tokenpak.status.snapshot`` (per
the PakLine / universal status-line architecture contract,
``decisions/2026-06-20-pakline-statusline-architecture-contract.md`` §2 + §5 —
"extract ``StatusCache``/``ProxyStatus`` from ``menu_status`` into
``tokenpak.status.snapshot``"). PakLine and other renderers consume the provider
directly; the interactive menu, ``doctor``, and ``_cli_core`` keep importing
``menu_status`` unchanged.

This module re-exports the proxy-status surface so every existing caller
(``menu.py``: ``snapshot`` / ``_port``; ``doctor.py``: ``snapshot``;
``_cli_core.py``: ``json_snapshot``; tests: ``ProxyStatus`` / ``StatusCache`` /
``reset_cache`` / ``STATUS_SCHEMA_VERSION``) resolves identically. ``__all__`` is
unchanged so the public-API snapshot is byte-stable.
"""

from __future__ import annotations

from tokenpak.status.snapshot import (
    STATUS_SCHEMA_VERSION as STATUS_SCHEMA_VERSION,
)
from tokenpak.status.snapshot import (
    ProxyStatus as ProxyStatus,
)
from tokenpak.status.snapshot import (
    StatusCache as StatusCache,
)
from tokenpak.status.snapshot import (
    _port as _port,
)
from tokenpak.status.snapshot import (
    json_snapshot as json_snapshot,
)
from tokenpak.status.snapshot import (
    reset_cache as reset_cache,
)
from tokenpak.status.snapshot import (
    snapshot as snapshot,
)

# Unchanged from the pre-extraction module — keeps the frozen public-API
# snapshot byte-identical (the snapshot records ``__all__`` names verbatim).
__all__ = ["ProxyStatus", "STATUS_SCHEMA_VERSION", "StatusCache", "reset_cache"]
