# SPDX-License-Identifier: Apache-2.0
"""Project scope resolution for vault retrieval.

Retrieval over a vault that holds several unrelated projects must never blend
them. A lexical scorer cannot tell two projects apart when their vocabulary
overlaps — "audit PR 100" is equally plausible in every one of them — so scope
has to be resolved *before* scoring, as a filter, not recovered afterwards as a
ranking signal. A boost cannot outrank dense term overlap from the wrong
project; a filter removes the wrong project from consideration entirely.

Project identity is **declared, never derived**. A directory path is not a
project: one project routinely spans a workbench, a staging checkout, an
archived copy and a notes tree, while unrelated projects share path shapes.
Membership is therefore an explicit relation between many roots and one project
id, declared in ``vault.yaml``::

    projects:
      - id: acme-storefront
        aliases: [acme, storefront]
        roots:
          - path: ~/workspace/acme-storefront
            role: workbench
          - path: ~/staging/acme-storefront
            role: staging
          - path: ~/archive/2025/acme-store-legacy
            role: archive
          - path: ~/vault/01_PROJECTS/acme
            role: notes
      - id: shared-design-system
        roots:
          - path: ~/workspace/design-system
            role: library
            shared: true          # visible to every scope

Two properties make this safe under overlap:

* **Longest-prefix wins.** Roots are matched by path specificity, so a nested
  root (``~/vault/01_PROJECTS/acme``) resolves ahead of a broader one
  (``~/vault``) regardless of declaration order.
* **Ambiguity is a load-time error, not a silent tiebreak.** Two roots that
  resolve the same path to different projects fail loudly at config load.
  Genuine multi-project resources are expressed *within one root* — either
  ``projects: [a, b]`` or ``shared: true`` — which is unambiguous by
  construction.

Standards: deterministic scope filtering over the vault file index is an
authorized OSS retrieval surface under the closed-allowlist boundary (single
source, no cross-source score normalization, no assembled package, no
autonomous promotion). Narrowing one store is not resolver-ranking.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

__all__ = [
    "SHARED",
    "ProjectRoot",
    "Project",
    "ProjectRegistry",
    "PathMembership",
    "ScopeResolution",
    "ScopeConflictError",
    "AmbiguityPolicy",
    "normalize_path",
]


SHARED = "*"
"""Sentinel project id for resources visible to every scope.

Stored as a real membership row so the query-time filter stays a single
``IN (?, '*')`` predicate rather than a special case in the SQL builder.
"""

_VALID_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

#: Roles that are excluded from retrieval unless explicitly requested. Archived
#: copies of a project are still *that* project — filtering them by role rather
#: than by project keeps them addressable without letting stale duplicates
#: outrank the live tree.
DEFAULT_EXCLUDED_ROLES: frozenset[str] = frozenset({"archive"})


class ScopeConflictError(ValueError):
    """Raised when the declared registry cannot resolve a path unambiguously."""


class AmbiguityPolicy:
    """What retrieval does when scope is unresolved and candidates span projects.

    ``SUPPRESS`` is the default because a wrong-project injection is worse than
    a missing one: it is silent, it looks authoritative, and the caller has no
    signal that the context came from somewhere else.
    """

    SUPPRESS = "suppress"
    DOMINANT = "dominant"
    UNSCOPED = "unscoped"

    ALL = (SUPPRESS, DOMINANT, UNSCOPED)


def normalize_path(path: str | os.PathLike[str]) -> str:
    """Expand, resolve and normalize *path* for prefix comparison."""
    return str(Path(path).expanduser().resolve(strict=False))


def _is_under(candidate: str, root: str) -> bool:
    """Return True if *candidate* is *root* or lives beneath it.

    Compares path components, so ``/srv/foo`` does not match root ``/srv/fo``
    the way a raw string prefix would.
    """
    if candidate == root:
        return True
    return candidate.startswith(root.rstrip(os.sep) + os.sep)


# ---------------------------------------------------------------------------
# Registry model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectRoot:
    """One declared directory and the project(s) it belongs to."""

    path: str
    role: str = "unspecified"
    projects: tuple[str, ...] = ()

    @property
    def is_shared(self) -> bool:
        return SHARED in self.projects

    @property
    def depth(self) -> int:
        """Path specificity, used to order longest-prefix matching."""
        return len(Path(self.path).parts)


@dataclass(frozen=True)
class Project:
    """A declared project identity."""

    id: str
    aliases: tuple[str, ...] = ()

    def match_terms(self) -> tuple[str, ...]:
        """Every literal that names this project in free text."""
        return (self.id, *self.aliases)


@dataclass(frozen=True)
class PathMembership:
    """The resolved membership of a single indexed path."""

    project_ids: tuple[str, ...]
    role: str
    matched_root: Optional[str]

    @property
    def resolved(self) -> bool:
        return bool(self.project_ids)


@dataclass(frozen=True)
class ScopeResolution:
    """The outcome of resolving a query's project scope."""

    project_id: Optional[str]
    source: str
    candidates: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.project_id is not None


@dataclass
class ProjectRegistry:
    """Declared projects and the roots that constitute them."""

    projects: dict[str, Project] = field(default_factory=dict)
    roots: tuple[ProjectRoot, ...] = ()

    # -- construction -----------------------------------------------------

    @classmethod
    def from_config(cls, raw_projects: Optional[Sequence[Mapping[str, object]]]) -> "ProjectRegistry":
        """Build a registry from the ``projects:`` block of ``vault.yaml``.

        Raises:
            ScopeConflictError: on a malformed declaration, an unknown project
                reference, or two roots that claim the same path for different
                projects.
        """
        if not raw_projects:
            return cls()

        projects: dict[str, Project] = {}
        roots: list[ProjectRoot] = []
        # normalized path -> the membership already claimed there
        claimed: dict[str, tuple[str, frozenset[str]]] = {}

        for item in raw_projects:
            if not isinstance(item, Mapping) or "id" not in item:
                raise ScopeConflictError(f"vault.yaml: invalid project entry: {item!r}")

            pid = str(item["id"]).strip().lower()
            if not _VALID_ID.match(pid):
                raise ScopeConflictError(
                    f"vault.yaml: invalid project id {pid!r} "
                    "(expected lowercase alphanumeric, '.', '_' or '-')"
                )
            if pid == SHARED:
                raise ScopeConflictError(f"vault.yaml: {SHARED!r} is reserved and cannot be a project id")
            if pid in projects:
                raise ScopeConflictError(f"vault.yaml: duplicate project id {pid!r}")

            aliases = _str_tuple(item.get("aliases"))
            projects[pid] = Project(id=pid, aliases=aliases)

            for raw_root in _iter_roots(item.get("roots"), pid):
                root = _build_root(raw_root, owner=pid)
                prior = claimed.get(root.path)
                members = frozenset(root.projects)
                if prior is not None and prior[1] != members:
                    raise ScopeConflictError(
                        f"vault.yaml: path {root.path!r} is claimed by "
                        f"{_fmt(prior[1])} and again by {_fmt(members)}. "
                        "Declare shared resources once with "
                        "'projects: [a, b]' or 'shared: true' instead of "
                        "repeating the root under each project."
                    )
                claimed[root.path] = (pid, members)
                roots.append(root)

        # Every referenced project must be declared — a typo in a multi-project
        # root would otherwise create a silent phantom scope that matches
        # nothing and filters everything.
        declared = set(projects) | {SHARED}
        for root in roots:
            unknown = [p for p in root.projects if p not in declared]
            if unknown:
                raise ScopeConflictError(
                    f"vault.yaml: root {root.path!r} references undeclared "
                    f"project(s): {', '.join(sorted(unknown))}"
                )

        # Deepest first, then longest string, then path — deterministic and
        # independent of declaration order.
        ordered = tuple(sorted(roots, key=lambda r: (-r.depth, -len(r.path), r.path)))
        return cls(projects=projects, roots=ordered)

    # -- path -> project --------------------------------------------------

    def resolve_path(self, path: str | os.PathLike[str]) -> PathMembership:
        """Resolve an indexed path to its declared membership.

        Longest-prefix wins. Shared roots contribute the ``*`` sentinel so a
        block under them is retrievable from every scope.
        """
        if not self.roots:
            return PathMembership(project_ids=(), role="unspecified", matched_root=None)

        target = normalize_path(path)
        for root in self.roots:
            if _is_under(target, root.path):
                return PathMembership(
                    project_ids=root.projects,
                    role=root.role,
                    matched_root=root.path,
                )
        return PathMembership(project_ids=(), role="unspecified", matched_root=None)

    # -- query -> scope ---------------------------------------------------

    def resolve_scope(
        self,
        *,
        explicit: Optional[str] = None,
        cwd: Optional[str | os.PathLike[str]] = None,
        query: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> ScopeResolution:
        """Resolve the project scope for a query.

        Order — first confident hit wins:

        1. ``explicit`` — a caller-supplied ``project=`` argument.
        2. ``$TOKENPAK_PROJECT`` — a pinned session scope.
        3. ``cwd`` — the caller's working directory, matched against roots.
        4. A project id or alias named literally in *query* text.
        5. Unresolved.

        Step 4 deliberately requires a *unique* mention. A query naming two
        projects is ambiguous, and guessing between them is exactly the failure
        this module exists to prevent.
        """
        if explicit:
            pid = explicit.strip().lower()
            if pid not in self.projects:
                raise ScopeConflictError(
                    f"unknown project {explicit!r}; declared: "
                    f"{', '.join(sorted(self.projects)) or '(none)'}"
                )
            return ScopeResolution(project_id=pid, source="explicit")

        environ = os.environ if env is None else env
        pinned = (environ.get("TOKENPAK_PROJECT") or "").strip().lower()
        if pinned:
            if pinned not in self.projects:
                raise ScopeConflictError(
                    f"TOKENPAK_PROJECT={pinned!r} is not a declared project"
                )
            return ScopeResolution(project_id=pinned, source="env")

        if cwd is not None:
            membership = self.resolve_path(cwd)
            # A shared root says nothing about which project the caller means.
            real = [p for p in membership.project_ids if p != SHARED]
            if len(real) == 1:
                return ScopeResolution(project_id=real[0], source="cwd")
            if len(real) > 1:
                return ScopeResolution(
                    project_id=None, source="cwd_ambiguous", candidates=tuple(sorted(real))
                )

        if query:
            named = self.projects_named_in(query)
            if len(named) == 1:
                return ScopeResolution(project_id=named[0], source="query")
            if len(named) > 1:
                return ScopeResolution(
                    project_id=None, source="query_ambiguous", candidates=named
                )

        return ScopeResolution(project_id=None, source="unresolved")

    def projects_named_in(self, text: str) -> tuple[str, ...]:
        """Return the declared projects named literally in *text*.

        Matching is word-boundary anchored and case-insensitive so ``acme`` hits
        "audit acme PR 100" but not "acmecorp".
        """
        if not text or not self.projects:
            return ()
        hits: set[str] = set()
        lowered = text.lower()
        for project in self.projects.values():
            for term in project.match_terms():
                if re.search(rf"(?<![\w-]){re.escape(term.lower())}(?![\w-])", lowered):
                    hits.add(project.id)
                    break
        return tuple(sorted(hits))

    # -- helpers ----------------------------------------------------------

    @property
    def active(self) -> bool:
        """True when at least one project is declared."""
        return bool(self.projects)

    def known(self, project_id: str) -> bool:
        return project_id.strip().lower() in self.projects


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _fmt(members: Iterable[str]) -> str:
    return ", ".join(sorted(members)) or "(none)"


def _str_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip().lower(),)
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip().lower() for v in value if str(v).strip())
    raise ScopeConflictError(f"vault.yaml: expected a string or list, got {value!r}")


def _iter_roots(value: object, pid: str) -> list[Mapping[str, object]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ScopeConflictError(f"vault.yaml: project {pid!r}: roots must be a list")
    out: list[Mapping[str, object]] = []
    for item in value:
        if isinstance(item, str):
            out.append({"path": item})
        elif isinstance(item, Mapping):
            out.append(item)
        else:
            raise ScopeConflictError(f"vault.yaml: project {pid!r}: invalid root {item!r}")
    return out


def _build_root(raw: Mapping[str, object], *, owner: str) -> ProjectRoot:
    if "path" not in raw:
        raise ScopeConflictError(f"vault.yaml: project {owner!r}: root missing 'path'")

    members: list[str] = []
    declared = _str_tuple(raw.get("projects"))
    members.extend(declared or (owner,))
    if bool(raw.get("shared", False)):
        members.append(SHARED)

    # Deduplicate while preserving order so the stored rows are stable.
    seen: set[str] = set()
    ordered = tuple(m for m in members if not (m in seen or seen.add(m)))

    return ProjectRoot(
        path=normalize_path(str(raw["path"])),
        role=str(raw.get("role", "unspecified")).strip().lower() or "unspecified",
        projects=ordered,
    )
