# SPDX-License-Identifier: Apache-2.0
"""Backward-compatible shim for the interactive-menu status source.

The cached, non-blocking proxy-status probe now lives in
``tokenpak.status.snapshot``. This module re-exports the existing proxy-status
names so the interactive menu, doctor command, CLI core, and tests keep their
imports unchanged.
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

# Keep the frozen public-API snapshot stable: it records these names verbatim.
__all__ = ["ProxyStatus", "STATUS_SCHEMA_VERSION", "StatusCache", "reset_cache"]
