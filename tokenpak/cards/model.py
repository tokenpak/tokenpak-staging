# SPDX-License-Identifier: Apache-2.0
"""Cards authoring-layer data model (Std 54 §B/§C).

Pure data: card kinds, the parsed-card record, validation issues, and
the shared vocabulary constants the parser/validator/compiler agree on.
No I/O and no imports from heavy subsystems — this module must stay
cheap to import so ``tokenpak --help`` stays fast.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

# ---------------------------------------------------------------------------
# Vocabulary (Std 54 §B/§C)
# ---------------------------------------------------------------------------

#: Card source format identifier (Std 54 §C ``source_format``).
SOURCE_FORMAT = "tokenpak-card-md-1"

#: ``card_kind`` enum, Phase 1 (Std 54 §B). Phase 2 adds ``worker``.
PHASE1_CARD_KINDS = ("tip", "pak")

#: Phase-2 card kinds / tip kinds — recognized so we can reject them with
#: a clear message instead of a generic parse error (Std 54 §B).
PHASE2_CARD_KINDS = ("worker",)
PHASE2_TIP_KINDS = ("context_connector", "mcp_bridge")

#: Phase-1 tip kinds (Std 54 §B).
PHASE1_TIP_KINDS = ("provider_adapter",)

#: ``card_status`` lifecycle values (Std 54 §C).
CARD_STATUSES = ("draft", "local", "installed")

#: Canonical compile targets per Phase-1 card kind (Std 54 §C/§D).
TARGET_CONTRACT_PAK = "tokenpak.tip.Pak"
TARGET_CONTRACT_PROVIDER_ADAPTER = (
    "tokenpak.services.routing_service.CredentialProvider"
)

#: Schema stamps for compiled output (Std 54 §C "Compile-output schema
#: stamping"). ``pak-v1.json`` is the registry mirror of the Pak contract
#: versioned in :mod:`tokenpak.tip.pak`; capability declarations validate
#: against the TIP capabilities schema. Only capability-bearing cards
#: stamp the capabilities schema.
PAK_TARGET_SCHEMA = "pak-v1.json"
CAPABILITIES_SCHEMA = "tip-capabilities.v1.json"

#: Compiled manifest envelope version (authoring-layer, not a TIP version).
MANIFEST_VERSION = 1

#: Card file suffixes by kind (Std 54 §K).
CARD_SUFFIXES = {
    ".tip.md": "tip",
    ".pak.md": "pak",
}

#: Project-local layout (Std 54 §K). Committed sources:
PROJECT_INDEX_FILENAME = ".tokenpak.md"
PROJECT_TIP_DIR = "integrations"
PROJECT_PAK_DIR = "paks"

#: Project-local generated state (gitignored):
PROJECT_STATE_DIR = ".tokenpak"
PROJECT_COMPILED_SUBPATH = ("cache", "cards", "compiled")
PROJECT_INSTALLED_SUBPATH = ("cache", "cards", "installed.json")

#: Trust / discovery modes (Std 54 §G).
MODE_DEV = "dev"
MODE_LOCKED = "locked"
MODES = (MODE_DEV, MODE_LOCKED)


# ---------------------------------------------------------------------------
# Errors / records
# ---------------------------------------------------------------------------


class CardError(Exception):
    """User-facing authoring-layer error (exit code 1 at the CLI)."""


@dataclass(frozen=True)
class ValidationIssue:
    """One validation finding.

    ``severity`` is ``error`` (blocks compile/install) or ``warning``
    (allowed in dev discovery per Std 54 §G).
    """

    severity: str  # "error" | "warning"
    code: str
    message: str

    def render(self) -> str:
        icon = "✗" if self.severity == "error" else "⚠"
        return f"{icon} [{self.code}] {self.message}"


@dataclass
class CardValidationReport:
    """Aggregated validation result for one card."""

    path: Optional[str] = None
    name: Optional[str] = None
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, code: str, message: str) -> None:
        self.issues.append(ValidationIssue("error", code, message))

    def warning(self, code: str, message: str) -> None:
        self.issues.append(ValidationIssue("warning", code, message))

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "ok": self.ok,
            "errors": [i.message for i in self.errors],
            "warnings": [i.message for i in self.warnings],
            "issues": [
                {"severity": i.severity, "code": i.code, "message": i.message}
                for i in self.issues
            ],
        }


@dataclass(frozen=True)
class ParsedCard:
    """A parsed (NOT yet validated) card source file.

    ``frontmatter`` is the ``yaml.safe_load`` result of the frontmatter
    block; ``body`` is the raw Markdown after the closing delimiter.
    The body is opaque text — it is never executed or imported
    (Std 54 §A invariant 2).
    """

    path: Optional[str]
    source_text: str
    source_sha256: str
    frontmatter: Mapping[str, Any]
    body: str

    @property
    def card_kind(self) -> Optional[str]:
        v = self.frontmatter.get("card_kind")
        return v if isinstance(v, str) else None

    @property
    def tip_kind(self) -> Optional[str]:
        v = self.frontmatter.get("tip_kind")
        return v if isinstance(v, str) else None

    @property
    def name(self) -> Optional[str]:
        v = self.frontmatter.get("name")
        return v if isinstance(v, str) else None

    @property
    def capabilities(self) -> list[str]:
        """Declared capability strings (may be invalid until validated)."""
        v = self.frontmatter.get("capabilities")
        if isinstance(v, list):
            return [c for c in v if isinstance(c, str)]
        return []

    def body_title(self) -> Optional[str]:
        """First ATX H1 heading in the body, if any."""
        for line in self.body.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()
        return None


__all__ = [
    "CARD_STATUSES",
    "CARD_SUFFIXES",
    "CAPABILITIES_SCHEMA",
    "CardError",
    "CardValidationReport",
    "MANIFEST_VERSION",
    "MODE_DEV",
    "MODE_LOCKED",
    "MODES",
    "PAK_TARGET_SCHEMA",
    "PHASE1_CARD_KINDS",
    "PHASE1_TIP_KINDS",
    "PHASE2_CARD_KINDS",
    "PHASE2_TIP_KINDS",
    "PROJECT_COMPILED_SUBPATH",
    "PROJECT_INDEX_FILENAME",
    "PROJECT_INSTALLED_SUBPATH",
    "PROJECT_PAK_DIR",
    "PROJECT_STATE_DIR",
    "PROJECT_TIP_DIR",
    "ParsedCard",
    "SOURCE_FORMAT",
    "TARGET_CONTRACT_PAK",
    "TARGET_CONTRACT_PROVIDER_ADAPTER",
    "ValidationIssue",
]
