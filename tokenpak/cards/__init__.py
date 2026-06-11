# SPDX-License-Identifier: Apache-2.0
"""TokenPak Cards — Markdown authoring layer for TIP and PAK (Std 54).

Cards are Markdown source files (``.tip.md`` / ``.pak.md``) that compile
into canonical TokenPak contracts. They make TIP and PAK easier to
author, but the runtime only trusts validated compiled JSON manifests.

Load-bearing invariants (Std 54 §A):

* Cards are source files, not runtime contracts.
* Raw Markdown is never executed — frontmatter is parsed with
  ``yaml.safe_load`` only and the body is treated as opaque text.
* Every active card must compile to a canonical ``target_contract``;
  no canonical target, no runtime effect.
* Capability strings come from :mod:`tokenpak.tip.capabilities` only.
* ``tokenpak cards`` operates on authoring sources; ``tokenpak pak``
  operates on runtime Pak objects. They are not aliases.

Phase 1 card kinds:

* ``.pak.md`` → :class:`tokenpak.tip.pak.Pak` manifest
* ``.tip.md tip_kind: provider_adapter`` → provider adapter manifest
  (Std 23 §1.2 ``{Vendor}{Product}CredentialProvider`` naming)

Phase 2 kinds (``context_connector`` / ``mcp_bridge`` / ``.worker.md``)
are recognized and rejected with a clear message — their canonical
contracts have not been ratified yet.
"""

from tokenpak.cards.compile import compile_card, write_compiled
from tokenpak.cards.discover import discover_cards, load_installed, record_install
from tokenpak.cards.model import (
    CardError,
    CardValidationReport,
    ParsedCard,
    ValidationIssue,
)
from tokenpak.cards.parser import parse_card_file, parse_card_text
from tokenpak.cards.scaffold import scaffold_card
from tokenpak.cards.validate import check_consistency, validate_card

__all__ = [
    "CardError",
    "CardValidationReport",
    "ParsedCard",
    "ValidationIssue",
    "check_consistency",
    "compile_card",
    "discover_cards",
    "load_installed",
    "parse_card_file",
    "parse_card_text",
    "record_install",
    "scaffold_card",
    "validate_card",
    "write_compiled",
]
