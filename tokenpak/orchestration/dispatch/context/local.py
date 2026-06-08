"""LocalContextProvider — OSS, deterministic context assembly (§5.9).

Implements the v0.1-alpha ``LocalContextProvider`` from Standards Delta v0 §5.9:

  inputs   : explicit files, Route/Station files, simple repo scan hints,
             current task frontmatter (if attached), manually attached context
  filters  : gitignore-aware path filtering, per-station size budget,
             per-station token budget (inherits Std 29 Spend Guard)
  guarantees: deterministic given the same inputs; no LLM call; no network call;
             no Std 32 Pak system dependency

Everything here is pure-Python and dependency-free (no ``pathspec``; gitignore
is matched with a small stdlib ``fnmatch``-based translator, consistent with the
existing ignore handling in ``tokenpak/compression/core.py``). Token counting is
a deterministic character-based approximation — no model tokenizer is invoked,
preserving the no-LLM / no-network guarantee. The live Spend-Guard-derived cap
is injected by the station runner via ``token_budget``; the module default is a
safe fallback.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from tokenpak.orchestration.dispatch.models.manifest import DispatchManifest
from tokenpak.orchestration.dispatch.models.route import RouteStation

from .provider import ContextBundle, ContextFile, ContextSource, source_rank

# Std 29 Spend Guard inheritance (Standards Delta v0 §5.9 + §8): the per-station
# token budget is supplied by the station runner from the live Spend Guard cap.
# These module defaults are conservative fallbacks for direct/standalone use and
# for tests; they are NOT a second budget hierarchy (§8 forbids that).
DEFAULT_STATION_SIZE_BUDGET_BYTES: int = 256 * 1024  # 256 KiB
DEFAULT_STATION_TOKEN_BUDGET: int = 32_000

# Deterministic token approximation: ~4 chars/token. Documented heuristic, not a
# model tokenizer (keeps the no-LLM guarantee). Empty content costs 0 tokens.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Deterministic, model-free token estimate for ``text``.

    Uses a fixed ~4-chars-per-token heuristic. Pure function of its input, so
    identical content always yields the same estimate (§5.9 determinism).
    """

    if not text:
        return 0
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))


class GitignoreMatcher:
    """Minimal, deterministic ``.gitignore`` matcher (stdlib only).

    Supports the patterns Dispatch context assembly needs: comments (``#``),
    blank lines, basename globs (``*.log``), anchored patterns (``/build``),
    nested paths (``a/b``), directory-only patterns (``build/``), ``**`` spans,
    and negation (``!keep.log``). Last matching rule wins, mirroring gitignore
    semantics; a file under an ignored directory stays ignored (negation cannot
    re-include it). This is intentionally not a full gitignore engine, but it is
    deterministic and dependency-free.
    """

    __slots__ = ("_rules",)

    def __init__(self, patterns: Sequence[str]):
        rules: list[tuple[bool, re.Pattern[str], bool]] = []
        for raw in patterns:
            line = raw.rstrip("\n").rstrip("\r")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            negated = stripped.startswith("!")
            if negated:
                stripped = stripped[1:]
            dir_only = stripped.endswith("/")
            if dir_only:
                stripped = stripped[:-1]
            anchored = stripped.startswith("/") or ("/" in stripped)
            stripped = stripped.lstrip("/")
            if not stripped:
                continue
            rules.append((negated, self._compile(stripped, anchored), dir_only))
        self._rules = rules

    @staticmethod
    def _compile(pattern: str, anchored: bool) -> re.Pattern[str]:
        """Translate a gitignore glob into an anchored regex over POSIX paths."""

        i = 0
        out: list[str] = []
        n = len(pattern)
        while i < n:
            c = pattern[i]
            if c == "*":
                if pattern[i : i + 2] == "**":
                    # '**/' spans zero or more directories; bare '**' spans all.
                    if pattern[i : i + 3] == "**/":
                        out.append("(?:.*/)?")
                        i += 3
                        continue
                    out.append(".*")
                    i += 2
                    continue
                out.append("[^/]*")
            elif c == "?":
                out.append("[^/]")
            else:
                out.append(re.escape(c))
            i += 1
        body = "".join(out)
        if anchored:
            regex = f"^{body}(?:/.*)?$"
        else:
            # Match the basename at any depth.
            regex = f"(?:^|.*/){body}(?:/.*)?$"
        return re.compile(regex)

    def is_ignored(self, rel_path: str) -> bool:
        """Return ``True`` if ``rel_path`` (POSIX, repo-relative) is ignored."""

        decision = False
        for negated, rule, _dir_only in self._rules:
            if rule.match(rel_path):
                decision = not negated
        return decision

    @classmethod
    def from_repo(cls, repo_root: Path) -> "GitignoreMatcher":
        """Build a matcher from ``<repo_root>/.gitignore`` (empty if absent)."""

        gitignore = repo_root / ".gitignore"
        if gitignore.is_file():
            try:
                patterns = gitignore.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                patterns = []
        else:
            patterns = []
        return cls(patterns)


class LocalContextProvider:
    """OSS deterministic ContextProvider (Standards Delta v0 §5.9).

    Construct with the repo root and per-station budgets, then call
    :meth:`build_context`. The variable per-request inputs (explicit files, route
    /station files, repo-scan hints, task frontmatter, attachments) are passed as
    keyword-only arguments; the bare ``build_context(manifest, station)`` call
    required by the :class:`ContextProvider` Protocol is fully supported.
    """

    def __init__(
        self,
        repo_root: Path | str,
        *,
        size_budget_bytes: int = DEFAULT_STATION_SIZE_BUDGET_BYTES,
        token_budget: int = DEFAULT_STATION_TOKEN_BUDGET,
        follow_gitignore: bool = True,
    ) -> None:
        if size_budget_bytes < 0 or token_budget < 0:
            raise ValueError("budgets must be non-negative")
        self.repo_root = Path(repo_root)
        self.size_budget_bytes = size_budget_bytes
        self.token_budget = token_budget
        self.follow_gitignore = follow_gitignore

    # -- public API ---------------------------------------------------------

    def build_context(
        self,
        manifest: DispatchManifest,
        station: RouteStation,
        *,
        explicit_files: Sequence[str] | None = None,
        station_files: Sequence[str] | None = None,
        repo_scan: Sequence[str] | None = None,
        task_frontmatter: Mapping[str, object] | str | None = None,
        attached: Mapping[str, str] | None = None,
    ) -> ContextBundle:
        """Assemble a deterministic, budget-bounded :class:`ContextBundle`.

        Args:
            manifest: the scoped work contract (provides ``manifest_id``).
            station: the route station this context is for (provides
                ``station_id``).
            explicit_files: repo-relative paths the user/request named.
            station_files: repo-relative paths declared by route/station config.
            repo_scan: simple glob hints for a repo scan (no semantic ranking).
            task_frontmatter: current task frontmatter, attached verbatim.
            attached: name -> content for manually attached, in-memory context.

        Disk-path sources are gitignore-filtered (when ``follow_gitignore``);
        in-memory sources (frontmatter, attachments) are not path-filtered.
        Items are de-duplicated by path with source precedence, ordered
        deterministically, then included greedily until either budget would be
        exceeded — overflow items are recorded in ``omitted_paths`` and set
        ``truncated``.
        """

        matcher: GitignoreMatcher | None = None
        if self.follow_gitignore:
            matcher = GitignoreMatcher.from_repo(self.repo_root)

        omitted: list[str] = []
        # path -> ContextFile, keeping the highest-precedence source per path.
        collected: dict[str, ContextFile] = {}

        def _add_disk(paths: Sequence[str] | None, source: ContextSource) -> None:
            for rel in paths or ():
                rel_posix = Path(rel).as_posix()
                if matcher is not None and matcher.is_ignored(rel_posix):
                    omitted.append(rel_posix)
                    continue
                abs_path = self.repo_root / rel_posix
                if not abs_path.is_file():
                    omitted.append(rel_posix)
                    continue
                try:
                    content = abs_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    omitted.append(rel_posix)
                    continue
                self._consider(collected, rel_posix, source, content)

        # 1. explicit, 2. route/station, in precedence order.
        _add_disk(explicit_files, ContextSource.EXPLICIT)
        _add_disk(station_files, ContextSource.ROUTE_STATION)

        # 3. task frontmatter (in-memory, attached verbatim).
        if task_frontmatter is not None:
            self._consider(
                collected,
                "<task frontmatter>",
                ContextSource.TASK_FRONTMATTER,
                _frontmatter_to_text(task_frontmatter),
            )

        # 4. manually attached context (in-memory).
        if attached:
            for name in sorted(attached):
                self._consider(
                    collected,
                    name,
                    ContextSource.ATTACHED,
                    attached[name],
                )

        # 5. simple repo scan (no semantic ranking) — lowest precedence.
        for rel_posix in self._scan_repo(repo_scan, matcher):
            abs_path = self.repo_root / rel_posix
            try:
                content = abs_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                omitted.append(rel_posix)
                continue
            self._consider(collected, rel_posix, ContextSource.REPO_SCAN, content)

        ordered = sorted(
            collected.values(), key=lambda cf: (source_rank(cf.source), cf.path)
        )

        files, total_bytes, total_tokens, truncated, dropped = self._apply_budgets(
            ordered
        )
        omitted.extend(dropped)

        bundle_id = ContextBundle.compute_id(manifest.id, station.id, files)
        return ContextBundle(
            id=bundle_id,
            manifest_id=manifest.id,
            station_id=station.id,
            files=files,
            total_size_bytes=total_bytes,
            total_estimated_tokens=total_tokens,
            size_budget_bytes=self.size_budget_bytes,
            token_budget=self.token_budget,
            truncated=truncated,
            omitted_paths=_dedupe_preserve_order(omitted),
        )

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _consider(
        collected: dict[str, ContextFile],
        path: str,
        source: ContextSource,
        content: str,
    ) -> None:
        """Insert ``path`` keeping the highest-precedence source on collision."""

        existing = collected.get(path)
        if existing is not None and source_rank(existing.source) <= source_rank(source):
            return
        collected[path] = ContextFile(
            path=path,
            source=source,
            content=content,
            size_bytes=len(content.encode("utf-8")),
            estimated_tokens=estimate_tokens(content),
        )

    def _scan_repo(
        self,
        repo_scan: Sequence[str] | None,
        matcher: GitignoreMatcher | None,
    ) -> list[str]:
        """Deterministically glob ``repo_scan`` hints under the repo root.

        No semantic ranking (§5.9): results are returned in sorted POSIX-path
        order. gitignore-matched and out-of-tree paths are skipped.
        """

        if not repo_scan:
            return []
        found: set[str] = set()
        for pattern in repo_scan:
            for abs_path in self.repo_root.glob(pattern):
                if not abs_path.is_file():
                    continue
                try:
                    rel_posix = abs_path.relative_to(self.repo_root).as_posix()
                except ValueError:
                    continue  # outside the repo root — skip
                if matcher is not None and matcher.is_ignored(rel_posix):
                    continue
                found.add(rel_posix)
        return sorted(found)

    def _apply_budgets(
        self, ordered: list[ContextFile]
    ) -> tuple[list[ContextFile], int, int, bool, list[str]]:
        """Greedily include files within both budgets; record overflow drops."""

        kept: list[ContextFile] = []
        dropped: list[str] = []
        total_bytes = 0
        total_tokens = 0
        truncated = False
        for cf in ordered:
            if (
                total_bytes + cf.size_bytes > self.size_budget_bytes
                or total_tokens + cf.estimated_tokens > self.token_budget
            ):
                truncated = True
                dropped.append(cf.path)
                continue
            kept.append(cf)
            total_bytes += cf.size_bytes
            total_tokens += cf.estimated_tokens
        return kept, total_bytes, total_tokens, truncated, dropped


def _frontmatter_to_text(frontmatter: Mapping[str, object] | str) -> str:
    """Render task frontmatter to a stable text form (deterministic).

    A string is used verbatim. A mapping is rendered as ``key: value`` lines
    sorted by key so identical frontmatter always serialises identically.
    """

    if isinstance(frontmatter, str):
        return frontmatter
    return "\n".join(f"{key}: {frontmatter[key]}" for key in sorted(frontmatter))


def _dedupe_preserve_order(items: Sequence[str]) -> list[str]:
    """Stable de-duplication preserving first-seen order."""

    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


__all__ = [
    "DEFAULT_STATION_SIZE_BUDGET_BYTES",
    "DEFAULT_STATION_TOKEN_BUDGET",
    "estimate_tokens",
    "GitignoreMatcher",
    "LocalContextProvider",
]
