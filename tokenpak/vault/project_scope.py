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
          - path: ~/project-notes/acme
            role: notes
      - id: shared-design-system
        roots:
          - path: ~/workspace/design-system
            role: library
            shared: true          # visible to every scope

Two properties make this safe under overlap:

* **Longest-prefix wins.** Roots are matched by path specificity, so a nested
  root (``~/project-notes/acme``) resolves ahead of a broader one
  (``~/project-notes``) regardless of declaration order.
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

import ast
import importlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Iterable, Mapping, Optional, Sequence

__all__ = [
    "SHARED",
    "ProjectRoot",
    "Project",
    "ProjectRegistry",
    "PathMembership",
    "ScopeResolution",
    "ScopeConflictError",
    "AmbiguityPolicy",
    "ScopeFilter",
    "ProjectScopeCapabilities",
    "ProjectScopeImplementation",
    "PROJECT_FILTERING_CAPABILITY",
    "SCOPE_RESOLUTION_CAPABILITY",
    "discover_project_scope_implementations",
    "spanned_projects",
    "normalize_path",
]


SHARED = "*"
"""Sentinel project id for resources visible to every scope.

Stored as a real membership row so the query-time filter stays a single
``IN (?, '*')`` predicate rather than a special case in the SQL builder.
"""


PROJECT_FILTERING_CAPABILITY = "project_filtering"
"""Explicit project filtering in ``search`` and ``compile_injection``."""

SCOPE_RESOLUTION_CAPABILITY = "scope_resolution"
"""Ambiguity-aware ``search_scoped`` resolution and fail-closed suppression."""


class ProjectScopeCapabilities:
    """Canonical capability contract for project-scoped retrieval.

    Implementations declare capabilities through ``project_scope_capabilities``;
    they do not redefine the meaning of the public properties below.

    ``supports_project_scope`` covers ``search(..., project=...)`` filtering
    before ``top_k`` and ``compile_injection(..., project=...)`` with the same
    membership rule. ``supports_scope_resolution`` additionally guarantees a
    ``search_scoped(...)`` method that resolves explicit, environment, cwd and
    query signals and suppresses unresolved multi-project matches.

    Shipping implementations set ``project_scope_implementation_id``. The
    conformance suite discovers those markers from package source, so adding a
    sibling cannot silently escape the shared regression matrix.
    """

    project_scope_implementation_id: ClassVar[str | None] = None
    project_scope_capabilities: ClassVar[frozenset[str]] = frozenset()

    @property
    def supports_project_scope(self) -> bool:
        """Whether explicit project filtering is implemented end to end."""
        return PROJECT_FILTERING_CAPABILITY in self.project_scope_capabilities

    @property
    def supports_scope_resolution(self) -> bool:
        """Whether ambiguity-aware ``search_scoped`` is implemented."""
        return SCOPE_RESOLUTION_CAPABILITY in self.project_scope_capabilities


@dataclass(frozen=True)
class ProjectScopeImplementation:
    """A shipping implementation found by the canonical source marker."""

    implementation_id: str
    implementation_class: type[ProjectScopeCapabilities]


def discover_project_scope_implementations() -> tuple[ProjectScopeImplementation, ...]:
    """Discover every shipping project-scope implementation in ``tokenpak``.

    Discovery reads class markers from Python source before importing only the
    matching modules. This avoids a central supported-backend list (which would
    drift) and avoids importing the entire package merely to enumerate three
    retrieval implementations. Duplicate ids and malformed declarations fail
    loudly because either would make the CI matrix incomplete or ambiguous.
    """

    package_root = Path(__file__).resolve().parents[1]
    discovered: list[ProjectScopeImplementation] = []
    seen_ids: set[str] = set()

    for source_path in sorted(package_root.rglob("*.py")):
        try:
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        except (OSError, SyntaxError):
            continue

        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            implementation_id: str | None = None
            for statement in node.body:
                if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = (
                    statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                )
                if not any(
                    isinstance(target, ast.Name) and target.id == "project_scope_implementation_id"
                    for target in targets
                ):
                    continue
                value = statement.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    implementation_id = value.value
                break

            if implementation_id is None:
                continue
            if implementation_id in seen_ids:
                raise RuntimeError(
                    f"duplicate project-scope implementation id: {implementation_id!r}"
                )

            relative = source_path.relative_to(package_root).with_suffix("")
            module_name = "tokenpak." + ".".join(relative.parts)
            module = importlib.import_module(module_name)
            implementation_class = getattr(module, node.name)
            if not isinstance(implementation_class, type) or not issubclass(
                implementation_class, ProjectScopeCapabilities
            ):
                raise RuntimeError(
                    f"{module_name}.{node.name} declares project-scope implementation "
                    "identity without inheriting ProjectScopeCapabilities"
                )
            seen_ids.add(implementation_id)
            discovered.append(ProjectScopeImplementation(implementation_id, implementation_class))

    return tuple(sorted(discovered, key=lambda item: item.implementation_id))


_VALID_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

#: Roles that are excluded from retrieval unless explicitly requested. Archived
#: copies of a project are still *that* project — filtering them by role rather
#: than by project keeps them addressable without letting stale duplicates
#: outrank the live tree.
DEFAULT_EXCLUDED_ROLES: frozenset[str] = frozenset({"archive"})


class ScopeConflictError(ValueError):
    """Raised when the declared registry cannot resolve a path unambiguously."""


def _scope_requested(
    *,
    explicit: Optional[str] = None,
    cwd: Optional[str | os.PathLike[str]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """Return whether a caller supplied any authoritative scope signal.

    Explicit ``project=``, the session's ``TOKENPAK_PROJECT`` pin, and ``cwd``
    are all requests to bind retrieval to one declared project. When no
    registry exists they must therefore fail together; checking only the
    explicit parameter silently drops the other two signals and returns an
    answer with false scope confidence.
    """
    environ = os.environ if env is None else env
    pinned = (environ.get("TOKENPAK_PROJECT") or "").strip()
    return bool((explicit or "").strip() or pinned or (str(cwd).strip() if cwd else ""))


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
    def from_config(
        cls, raw_projects: Optional[Sequence[Mapping[str, object]]]
    ) -> "ProjectRegistry":
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
        # normalized path -> the declaration that already claimed it. A root
        # has one role as well as one membership relation; accepting the same
        # path twice makes the role depend on declaration order and can turn an
        # archive exclusion into a workbench inclusion (or vice versa).
        claimed: dict[str, tuple[str, frozenset[str], str]] = {}

        for item in raw_projects:
            if not isinstance(item, Mapping) or "id" not in item:
                raise ScopeConflictError(f"vault.yaml: invalid project entry: {item!r}")

            raw_pid = item["id"]
            if not isinstance(raw_pid, str):
                raise ScopeConflictError(
                    f"vault.yaml: project id must be a string, got {raw_pid!r}"
                )
            pid = raw_pid.strip().lower()
            if not _VALID_ID.match(pid):
                raise ScopeConflictError(
                    f"vault.yaml: invalid project id {pid!r} "
                    "(expected lowercase alphanumeric, '.', '_' or '-')"
                )
            if pid == SHARED:
                raise ScopeConflictError(
                    f"vault.yaml: {SHARED!r} is reserved and cannot be a project id"
                )
            if pid in projects:
                raise ScopeConflictError(f"vault.yaml: duplicate project id {pid!r}")

            aliases = _str_tuple(item.get("aliases"), field=f"project {pid!r} aliases")
            projects[pid] = Project(id=pid, aliases=aliases)

            for raw_root in _iter_roots(item.get("roots"), pid):
                root = _build_root(raw_root, owner=pid)
                prior = claimed.get(root.path)
                members = frozenset(root.projects)
                if prior is not None:
                    raise ScopeConflictError(
                        f"vault.yaml: path {root.path!r} is declared more than once "
                        f"(first: projects={_fmt(prior[1])}, role={prior[2]!r}; "
                        f"again: projects={_fmt(members)}, role={root.role!r}). "
                        "Declare each normalized root once; express multi-project "
                        "membership in that declaration with 'projects: [a, b]' "
                        "or 'shared: true'."
                    )
                claimed[root.path] = (pid, members, root.role)
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
                raise ScopeConflictError(f"TOKENPAK_PROJECT={pinned!r} is not a declared project")
            return ScopeResolution(project_id=pinned, source="env")

        named = self.projects_named_in(query) if query else ()
        if len(named) > 1:
            return ScopeResolution(project_id=None, source="query_ambiguous", candidates=named)

        cwd_projects: tuple[str, ...] = ()
        if cwd is not None:
            membership = self.resolve_path(cwd)
            # A shared root says nothing about which project the caller means.
            cwd_projects = tuple(sorted(p for p in membership.project_ids if p != SHARED))
            if len(cwd_projects) > 1:
                return ScopeResolution(
                    project_id=None, source="cwd_ambiguous", candidates=cwd_projects
                )

        # Where the working directory and the query name *different* projects,
        # the working directory does not win. Letting it win produces the worst
        # available outcome: a coherent, confident, single-project answer about
        # the project the caller did not ask for — no blend to notice, no
        # candidates reported, nothing to signal the substitution. Someone
        # sitting in one checkout while asking about another is stating an
        # intent that contradicts their location, and that conflict is exactly
        # the condition this resolver must refuse rather than break.
        if cwd_projects and named and cwd_projects[0] != named[0]:
            return ScopeResolution(
                project_id=None,
                source="cwd_query_conflict",
                candidates=tuple(sorted({cwd_projects[0], named[0]})),
            )

        if cwd_projects:
            return ScopeResolution(project_id=cwd_projects[0], source="cwd")
        if named:
            return ScopeResolution(project_id=named[0], source="query")

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
# Backend-agnostic enforcement
# ---------------------------------------------------------------------------
#
# There are three separate retrieval implementations in the tree (the proxy's
# in-memory index, the SQLite backend, and the SDK's own index). A guarantee
# re-implemented once per backend is a guarantee that holds in whichever ones
# someone remembered. These two helpers are the shared enforcement primitives:
# a membership predicate and an ambiguity test, both pure functions of the
# registry and a source path.


class ScopeFilter:
    """Membership predicate over ``source_path``, for in-memory backends.

    The SQLite backend pushes the equivalent test into SQL so it runs before
    scoring; backends that rank in memory apply this while iterating candidates
    — in both cases *before* top-k truncation, so an out-of-scope block can
    never consume a result slot.

    An inactive registry admits everything, which keeps unscoped installs on
    exactly their previous behaviour.
    """

    __slots__ = ("_registry", "_project", "_excluded")

    def __init__(
        self,
        registry: "ProjectRegistry",
        project: Optional[str] = None,
        exclude_roles: Optional[Iterable[str]] = None,
    ) -> None:
        self._registry = registry
        self._project = project
        self._excluded = (
            frozenset(exclude_roles)
            if exclude_roles is not None
            else (DEFAULT_EXCLUDED_ROLES if registry.active else frozenset())
        )

    @property
    def active(self) -> bool:
        """True when this filter can reject anything."""
        return bool(self._project) or bool(self._excluded)

    def allows(self, source_path: str) -> bool:
        if not self.active:
            return True
        membership = self._registry.resolve_path(source_path)
        if self._excluded and membership.role in self._excluded:
            return False
        if not self._project:
            return True
        # SHARED is admitted under every scope by construction.
        return self._project in membership.project_ids or SHARED in membership.project_ids


def spanned_projects(registry: "ProjectRegistry", source_paths: Iterable[str]) -> tuple[str, ...]:
    """Projects genuinely in contention across *source_paths*.

    Only ``shared: true`` (the ``*`` sentinel) grants universal cover. Every
    other project id a block carries counts as contention.

    An earlier version treated this as a set-cover question — unambiguous when
    *some* id covered every result. That is wrong, and wrong in the unsafe
    direction. Given natural declarations like::

        acme: roots: [{path: .../acme, projects: [acme, monorepo]}]
        beta: roots: [{path: .../beta, projects: [beta, monorepo]}]

    every result is covered by ``monorepo``, so cover-based logic returns "not
    ambiguous" and interleaves acme and beta content with an empty ``spanned``
    — a blend with no signal attached, which is precisely what this module
    exists to prevent. Co-membership in an umbrella id means "both of these
    belong to it", not "these two are the same thing".

    The cost of the strict rule is that a result set landing *only* on a root
    declared ``projects: [a, b]`` is reported as contended and suppressed, even
    though returning it would have been defensible under either scope. That
    failure is closed — a refusal the caller can resolve by naming a project —
    and a closed failure is the correct trade here.
    """
    seen: set[str] = set()
    for path in source_paths:
        ids = registry.resolve_path(path).project_ids
        if not ids or SHARED in ids:
            continue  # unclaimed or universally shared — never evidence of contention
        seen |= {pid for pid in ids if pid != SHARED}
    return tuple(sorted(seen))


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _fmt(members: Iterable[str]) -> str:
    return ", ".join(sorted(members)) or "(none)"


def _str_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            raise ScopeConflictError(f"vault.yaml: {field} cannot contain an empty value")
        return (normalized,)
    if isinstance(value, (list, tuple)):
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ScopeConflictError(
                    f"vault.yaml: {field} values must be strings, got {item!r}"
                )
            clean = item.strip().lower()
            if not clean:
                raise ScopeConflictError(f"vault.yaml: {field} cannot contain an empty value")
            normalized.append(clean)
        return tuple(normalized)
    raise ScopeConflictError(
        f"vault.yaml: {field} must be a string or list of strings, got {value!r}"
    )


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
    path = raw["path"]
    if not isinstance(path, str) or not path.strip():
        raise ScopeConflictError(
            f"vault.yaml: project {owner!r}: root path must be a non-empty string, got {path!r}"
        )

    members: list[str] = []
    declared = _str_tuple(raw.get("projects"), field=f"project {owner!r} root projects")
    members.extend(declared or (owner,))
    shared = raw.get("shared", False)
    if not isinstance(shared, bool):
        raise ScopeConflictError(
            f"vault.yaml: project {owner!r}: root 'shared' must be a boolean, got {shared!r}"
        )
    if shared:
        members.append(SHARED)

    role = raw.get("role", "unspecified")
    if not isinstance(role, str):
        raise ScopeConflictError(
            f"vault.yaml: project {owner!r}: root role must be a string, got {role!r}"
        )

    # Deduplicate while preserving order so the stored rows are stable.
    seen: set[str] = set()
    ordered = tuple(m for m in members if not (m in seen or seen.add(m)))

    return ProjectRoot(
        path=normalize_path(path),
        role=role.strip().lower() or "unspecified",
        projects=ordered,
    )
