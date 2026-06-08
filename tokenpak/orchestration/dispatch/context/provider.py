"""ContextProvider interface + ContextBundle return type (Standards Delta v0 §5.9).

This module defines the *boundary* between TokenPak Dispatch's OSS context
assembly and the future Pro (``tokenpak-paid``) Context Package Builder. The
interface ships in Phase B from day one so the seam is visible; Pro activation
(Phase D) is a constructor swap of the :class:`ContextProvider` instance, not a
rewrite (Std 25 §1.1 + §9.3; Standards Delta v0 §5.9).

Contract (Standards Delta v0 §5.9)::

    class ContextProvider(Protocol):
        def build_context(
            self,
            manifest: DispatchManifest,
            station: DispatchStation,
        ) -> ContextBundle: ...

The Standards Delta names the station argument ``DispatchStation``; the
implemented station record is :class:`RouteStation` (Standards Delta v0 §4.3),
which is what a route hands to the runner. The Protocol below is typed against
``RouteStation`` accordingly.

The OSS implementation is :class:`tokenpak.orchestration.dispatch.context.local.
LocalContextProvider` — deterministic, no LLM, no network, no Std 32 Pak system
dependency. :class:`PaidContextProvider` is a stub that raises
``NotImplementedError`` if instantiated; it exists only to make the boundary
explicit.
"""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import Field

from tokenpak.orchestration.dispatch.models.common import DispatchBaseModel
from tokenpak.orchestration.dispatch.models.manifest import DispatchManifest
from tokenpak.orchestration.dispatch.models.route import RouteStation


class ContextSource(str, Enum):
    """Provenance of a context item (Standards Delta v0 §5.9 input list).

    The five sources mirror the §5.9 ``LocalContextProvider.inputs`` list. The
    enum order is also the deterministic precedence order used when the same
    path is supplied by more than one source (earlier wins):
    ``EXPLICIT > ROUTE_STATION > TASK_FRONTMATTER > ATTACHED > REPO_SCAN``.
    """

    EXPLICIT = "explicit"
    ROUTE_STATION = "route_station"
    TASK_FRONTMATTER = "task_frontmatter"
    ATTACHED = "attached"
    REPO_SCAN = "repo_scan"


# Deterministic precedence: lower index wins on path collision.
_SOURCE_PRECEDENCE: tuple[ContextSource, ...] = (
    ContextSource.EXPLICIT,
    ContextSource.ROUTE_STATION,
    ContextSource.TASK_FRONTMATTER,
    ContextSource.ATTACHED,
    ContextSource.REPO_SCAN,
)


def source_rank(source: ContextSource) -> int:
    """Precedence rank for ``source`` (lower = higher priority)."""

    return _SOURCE_PRECEDENCE.index(source)


class ContextFile(DispatchBaseModel):
    """A single piece of assembled context (a file, frontmatter, or attachment).

    ``path`` is a repo-relative POSIX path for disk-sourced items, or a stable
    synthetic label (e.g. ``"<task frontmatter>"``) for in-memory items. The
    ``size_bytes`` / ``estimated_tokens`` fields are computed from ``content``
    by the provider and counted against the per-station budgets.
    """

    path: str
    source: ContextSource
    content: str
    size_bytes: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)


class ContextBundle(DispatchBaseModel):
    """Assembled, budget-bounded context for one station run (§5.9 return type).

    Referenced by :class:`tokenpak.orchestration.dispatch.models.station_run.
    DispatchStationRun` via its ``context_bundle_id`` field. ``id`` is a
    deterministic content hash (see :meth:`compute_id`): identical inputs always
    produce an identical bundle, satisfying the §5.9 determinism guarantee.

    ``omitted_paths`` records items dropped by gitignore filtering or budget
    enforcement, in deterministic encounter order, so the drop is auditable
    rather than silent. ``truncated`` is ``True`` iff at least one item was
    dropped because it would have exceeded a budget.
    """

    id: str
    manifest_id: str
    station_id: str

    files: list[ContextFile] = Field(default_factory=list)

    total_size_bytes: int = Field(default=0, ge=0)
    total_estimated_tokens: int = Field(default=0, ge=0)

    size_budget_bytes: int = Field(ge=0)
    token_budget: int = Field(ge=0)

    truncated: bool = False
    omitted_paths: list[str] = Field(default_factory=list)

    @staticmethod
    def compute_id(
        manifest_id: str,
        station_id: str,
        files: list[ContextFile],
    ) -> str:
        """Deterministic ``ctxbundle_<hash>`` id derived from bundle contents.

        The hash covers the manifest id, station id, and each included file's
        ``(source, path, content)`` in list order. Because the provider emits
        files in a deterministic order, identical inputs yield an identical id
        — no timestamp or random component is used (Std-safe for replay).
        """

        hasher = hashlib.sha256()
        hasher.update(manifest_id.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(station_id.encode("utf-8"))
        for cf in files:
            hasher.update(b"\x00")
            hasher.update(cf.source.value.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(cf.path.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(cf.content.encode("utf-8"))
        return f"ctxbundle_{hasher.hexdigest()[:32]}"


@runtime_checkable
class ContextProvider(Protocol):
    """Boundary contract for context assembly (Standards Delta v0 §5.9).

    Implementations MUST be callable as ``build_context(manifest, station)``.
    Concrete providers may accept additional keyword-only inputs (the OSS
    :class:`LocalContextProvider` does, for explicit files / repo scan / task
    frontmatter / attachments) without breaking structural conformance.
    """

    def build_context(
        self,
        manifest: DispatchManifest,
        station: RouteStation,
    ) -> ContextBundle: ...


class PaidContextProvider:
    """Pro-path stub (Standards Delta v0 §5.9 "Pro path").

    The real implementation delegates to the ``tokenpak-paid`` Context Package
    Builder (Std 32 §1.3), activated when the Pro daemon is detected over
    loopback (Std 25 §9.3). It is **not** implemented in the OSS tree: per the
    OSS/Pro boundary (Std 25 §1.1) the Pro context behaviour ships only in
    ``tokenpak-paid``. Instantiating this stub raises ``NotImplementedError`` so
    the boundary fails loud if the OSS path is wired to Pro by mistake.

    Phase D activation swaps the :class:`ContextProvider` *instance* (Local →
    Paid) at the station runner; it is not a rewrite of this interface.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError(
            "PaidContextProvider is a Pro-path stub (Standards Delta v0 §5.9). "
            "The Context Package Builder ships in tokenpak-paid (Std 32 §1.3) "
            "and activates over loopback when the Pro daemon is detected "
            "(Std 25 §9.3). The OSS path uses LocalContextProvider; fall back "
            "to it when the Pro daemon is absent."
        )

    def build_context(
        self,
        manifest: DispatchManifest,
        station: RouteStation,
    ) -> ContextBundle:  # pragma: no cover - unreachable; __init__ always raises
        raise NotImplementedError


__all__ = [
    "ContextSource",
    "source_rank",
    "ContextFile",
    "ContextBundle",
    "ContextProvider",
    "PaidContextProvider",
]
