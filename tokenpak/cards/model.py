# SPDX-License-Identifier: Apache-2.0
"""TokenPak Cards — shared data model for the Markdown authoring layer.

Cards are Markdown source files (``.pak.md`` / ``.tip.md``) that compile
into canonical TokenPak contracts. They are forgiving authoring inputs;
the compiled JSON manifests are the strict runtime inputs.

Load-bearing rules implemented across this package:

1.  Cards are source files, not runtime contracts.
2.  Raw Markdown is never executed. Fenced code blocks inside a card body
    are inert content; the pipeline only ever parses YAML metadata blocks
    with ``yaml.safe_load``.
3.  Every active card must compile to a canonical ``target_contract``.
4.  No canonical target, no runtime effect.
5.  Card lifecycle status is ``card_status`` (draft | local | installed)
    and is distinct from any field of the compiled contract.
6.  Capability strings come from :mod:`tokenpak.tip.capabilities` only;
    ``ext.<vendor>.<feature>`` strings are vendor metadata hints that the
    core runtime must not base policy decisions on.
7.  ``tokenpak cards`` operates on authoring sources; ``tokenpak pak``
    operates on runtime Pak objects. They are not aliases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The authoring source format this pipeline understands.
SOURCE_FORMAT = "tokenpak-card-md-1"

#: Format stamp written into every compiled manifest.
COMPILED_MANIFEST_FORMAT = "tokenpak-card-compiled-1"

#: Canonical compile target for ``.pak.md`` cards.
PAK_TARGET_CONTRACT = "tokenpak.tip.Pak"

#: Schema family the compiled Pak payload validates against.
PAK_TARGET_SCHEMA = "pak-v1"

#: Card lifecycle states (authoring-side; never reuses compiled-contract fields).
CARD_STATUS_VALUES = ("draft", "local", "installed")

#: Card kinds this build can fully compile.
SUPPORTED_CARD_KINDS = ("pak",)

#: ``tip_kind`` values that are part of the contract but whose compile
#: target is not available in this build. The surface accepts them and
#: returns a clear error instead of silently ignoring the verb.
NOT_YET_SUPPORTED_TIP_KINDS = ("provider_adapter",)

#: ``tip_kind`` values deferred to a later phase (their target contracts
#: are not yet canonical).
LATER_PHASE_TIP_KINDS = ("context_connector", "mcp_bridge")

#: Card kinds deferred to a later phase entirely.
LATER_PHASE_CARD_KINDS = ("worker",)

#: File suffix → card kind. ``.worker.md`` is recognized during discovery
#: so it can be reported as unsupported rather than invisibly skipped.
CARD_SUFFIXES: Mapping[str, str] = {
    ".pak.md": "pak",
    ".tip.md": "tip",
    ".worker.md": "worker",
}

#: Project index file. Project-tree (dev) discovery is gated on its
#: presence; it is an index file, not a card.
PROJECT_INDEX_FILENAME = ".tokenpak.md"

#: Project-local state directory (analogous to ``.git/`` — project scope,
#: long-form name; the user-global home keeps its own short-form name).
PROJECT_STATE_DIRNAME = ".tokenpak"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CardError(Exception):
    """Base class for authoring-layer failures. ``code`` is machine-readable."""

    code = "card-error"

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class CardParseError(CardError):
    """The card file is structurally unreadable (frontmatter/YAML level)."""

    code = "card-parse-error"


class CardValidationError(CardError):
    """Validation produced blocking errors. Carries the issue list."""

    code = "card-validation-error"

    def __init__(self, message: str, issues: "tuple[CardIssue, ...]" = ()) -> None:
        super().__init__(message)
        self.issues = tuple(issues)


class UnsupportedCardKindError(CardError):
    """The card kind is recognized by the contract but cannot be built here.

    Used both for kinds whose compile target lands in a follow-up release
    and for kinds deferred to a later phase.
    """

    code = "card-kind-not-yet-supported"


class ScaffoldGuardError(CardError):
    """Scaffold attempted to write inside the installed package tree."""

    code = "scaffold-package-tree-guard"


# ---------------------------------------------------------------------------
# Issue + parsed/compiled records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CardIssue:
    """A single validation finding.

    ``severity`` is one of ``error`` (blocks compile/install), ``warning``
    (surfaced; blocks only under ``--strict``), or ``info`` (advisory).
    """

    severity: str
    code: str
    message: str

    def render(self) -> str:
        icon = {"error": "❌", "warning": "⚠️ ", "info": "ℹ️ "}.get(self.severity, "•")
        return f"{icon} [{self.code}] {self.message}"


@dataclass
class ParsedCard:
    """A card after structural parsing, before semantic validation."""

    path: Optional[Path]
    raw_sha256: str
    frontmatter: dict[str, Any]
    title: str
    summary: str
    #: Recognized body metadata sections (``scope`` / ``source`` /
    #: ``anchors`` / ``relationships``), each the ``yaml.safe_load`` of the
    #: section's fenced YAML block. Everything else in the body is inert.
    sections: dict[str, Any] = field(default_factory=dict)
    #: Environment-variable names referenced in the body (static scan, for
    #: ``inspect`` reporting only — never resolved by this pipeline).
    env_refs: tuple[str, ...] = ()

    @property
    def card_kind(self) -> str:
        return str(self.frontmatter.get("card_kind", "") or "")

    @property
    def tip_kind(self) -> str:
        return str(self.frontmatter.get("tip_kind", "") or "")

    @property
    def name(self) -> str:
        return str(self.frontmatter.get("name", "") or "")

    @property
    def card_status(self) -> str:
        return str(self.frontmatter.get("card_status", "") or "")

    @property
    def capabilities(self) -> tuple[str, ...]:
        raw = self.frontmatter.get("capabilities") or []
        if isinstance(raw, str):
            raw = [raw]
        return tuple(str(c) for c in raw)


@dataclass
class CompiledCard:
    """Result of a successful compile: the contract object + its manifest."""

    card: ParsedCard
    target_contract: str
    manifest: dict[str, Any]
    #: The canonical contract instance (a ``tokenpak.tip.pak.Pak`` for
    #: ``.pak.md`` cards). Kept for in-process callers; the manifest is the
    #: durable form.
    contract: Any = None
