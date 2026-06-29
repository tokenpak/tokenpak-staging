# SPDX-License-Identifier: Apache-2.0
"""Scriptability / output contract for the curated CLI summary surface.

This module is the **single source of truth** for the curated CLI output
contract. It exists so that the scriptable behaviour of the curated command
surface is described in one place and pinned by conformance tests rather than
re-discovered per command.

The contract has three parts:

* **JSON** — summary/insight commands expose a stable ``--json`` mode that emits
  a single machine-readable document. The verbs in :data:`JSON_REQUIRED` MUST
  accept ``--json`` in the built parser; ``tests/cli/test_output_contract.py``
  fails if any regresses, and the Visibility-surface *partition* guard fails if
  a newly registered summary command is added without classifying its output
  contract.
* **Quiet** — ``--quiet`` / ``-q`` is a global prefix flag (handled by
  ``_cli_core._consume_global_prefix``) that suppresses presentation chrome. It
  must never hide errors or change exit-code semantics.
* **Leaf help** — every curated summary verb must carry, in the command
  registry, a purpose (``detail``), a runnable example with flags (``usage``)
  and related verbs (``related``); see ``tests/cli/test_leaf_help_rubric.py``.

Nothing here changes command *behaviour*. The :func:`emit` helper is the
canonical pattern new summary commands should adopt when wiring their own
output, so the json/quiet branches stay uniform across the surface.
"""

from __future__ import annotations

import json as _json
import sys as _sys
from typing import Any, Callable, Iterable

# No public re-exports: keep this module out of the package API snapshot. Tests
# and (future) command handlers import the names they need explicitly.
__all__: list[str] = []

# --- Curated summary surface ------------------------------------------------

#: Registry category that collects the read-only summary / insight surface.
SUMMARY_CATEGORY = "Visibility"

#: Summary verbs that MUST expose a stable local ``--json`` flag. These present
#: structured data a script or CI step legitimately consumes. The set spans a
#: few registry categories (Visibility insight verbs plus Control/Optimization
#: status verbs) because "summary" is about the output shape, not the category.
JSON_REQUIRED: tuple[str, ...] = (
    # Visibility insight verbs
    "cost",
    "doctor",
    "savings",
    "dashboard",
    "diff",
    "last",
    "report",
    # Cross-category status / measurement verbs
    "status",
    "cache",
    "compress",
    "benchmark",
)

#: Visibility verbs that are intentionally NOT required to expose ``--json`` at
#: the top level, each with the reason it is exempt. Keeping the exemptions
#: explicit means a *new* Visibility command cannot slip in unclassified: the
#: partition guard in the tests requires every Visibility verb to be either
#: required or exempt.
JSON_EXEMPT_VISIBILITY: dict[str, str] = {
    "stats": "human-oriented sparkline view; numeric data is available via cost/savings --json",
    "debug": "container verb; JSON lives on its leaves (e.g. debug receipt --json)",
    "replay": "side-effecting re-run, not a structured summary",
    "models": "static catalogue rendered as a table; not a per-run summary",
    "goals": "interactive goal editor, not a machine-readable report",
    "prove": "proof-bundle workflow with its own artifact format",
    "verify": "pass/fail check surfaced through the exit code, not a JSON body",
}


def load_command_registry() -> dict[str, Any]:
    """Load the canonical command registry (``commands.json``).

    Imported lazily and locally so this module has no import-time dependency on
    the registry package and never leaks names into ``tokenpak.cli``.
    """
    from importlib import resources

    data = (
        resources.files("tokenpak.core.registry")
        .joinpath("commands.json")
        .read_text(encoding="utf-8")
    )
    return _json.loads(data)


def registry_commands_in_category(category: str) -> list[str]:
    """Return the registry command names belonging to ``category``."""
    reg = load_command_registry()
    return [c["command"] for c in reg.get("commands", []) if c.get("category") == category]


def registry_entry(command: str) -> dict[str, Any] | None:
    """Return the registry entry for ``command`` or ``None`` if absent."""
    reg = load_command_registry()
    for entry in reg.get("commands", []):
        if entry.get("command") == command:
            return entry
    return None


# --- Parser introspection helpers ------------------------------------------


def _subparser_choices(parser) -> dict:
    import argparse

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _option_strings(parser) -> set[str]:
    opts: set[str] = set()
    for action in parser._actions:
        opts.update(action.option_strings)
    return opts


def command_accepts_json(parser, verb: str) -> bool:
    """True if ``verb``'s subparser accepts a local ``--json`` flag."""
    sub = _subparser_choices(parser).get(verb)
    if sub is None:
        return False
    return "--json" in _option_strings(sub)


# --- The canonical emit pattern --------------------------------------------


def emit(
    payload: Any,
    *,
    as_json: bool,
    quiet: bool = False,
    render: Callable[[Any], str] | None = None,
    render_quiet: Callable[[Any], str] | None = None,
    file=None,
) -> int:
    """Emit a summary command's result under the output contract.

    Parameters
    ----------
    payload:
        The structured result. In JSON mode it is serialised verbatim.
    as_json:
        When true, write a single ``json.dumps`` document and ignore the text
        renderers (presentation chrome). This is the scriptable path.
    quiet:
        When true (and not JSON), suppress presentation chrome: ``render`` is
        not called. ``render_quiet`` may supply a minimal one-line form.
    render / render_quiet:
        Callables turning ``payload`` into human text for the normal and quiet
        text paths respectively.
    file:
        Output stream (defaults to ``sys.stdout``).

    Returns ``0``. This helper never calls :func:`sys.exit` and never swallows
    exceptions raised by a renderer — exit-code and error semantics stay with
    the caller so ``--quiet`` cannot change them.
    """
    out = file if file is not None else _sys.stdout
    if as_json:
        out.write(_json.dumps(payload, sort_keys=True) + "\n")
        return 0
    if quiet:
        if render_quiet is not None:
            text = render_quiet(payload)
            if text:
                out.write(text if text.endswith("\n") else text + "\n")
        return 0
    if render is not None:
        text = render(payload)
        if text:
            out.write(text if text.endswith("\n") else text + "\n")
    return 0


def missing_rubric_fields(entry: dict[str, Any]) -> Iterable[str]:
    """Yield the leaf-help rubric fields missing from a registry ``entry``.

    The rubric: a curated summary verb's registry entry must carry a purpose
    (``detail``), a runnable example with flags (``usage``) and related verbs
    (``related``).
    """
    if not (entry.get("detail") or "").strip():
        yield "detail"
    usage = (entry.get("usage") or "").strip()
    if not usage or "tokenpak" not in usage:
        yield "usage"
    if not entry.get("related"):
        yield "related"
