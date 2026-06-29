# SPDX-License-Identifier: Apache-2.0
"""Render-agnostic status provider package.

Houses the canonical ``StatusSnapshot`` data contract and the extracted proxy
probe (architecture contract ``decisions/2026-06-20-pakline-statusline-architecture-contract.md``
§2 + §3.3). Renderers/adapters (PakLine native / tmux / title / ``--json``)
consume ``StatusSnapshot``; they never re-derive telemetry.

``__all__`` is intentionally empty so this internal plumbing does not enter the
frozen public-API snapshot. Consumers import the contract by explicit path,
e.g. ``from tokenpak.status.snapshot import StatusSnapshot, build_status_snapshot``.
The convenience re-exports below exist for ergonomics, not as a public surface.
"""

from __future__ import annotations

from tokenpak.status.snapshot import (
    RoutingMode as RoutingMode,
)
from tokenpak.status.snapshot import (
    StatusField as StatusField,
)
from tokenpak.status.snapshot import (
    StatusSnapshot as StatusSnapshot,
)
from tokenpak.status.snapshot import (
    StatusSource as StatusSource,
)
from tokenpak.status.snapshot import (
    build_status_snapshot as build_status_snapshot,
)

# Internal plumbing only — not a frozen public-API surface.
__all__: list[str] = []
