# SPDX-License-Identifier: Apache-2.0
"""Leaf-help rubric conformance for the curated CLI summary surface.

The v2 DX contract (audit B1/B2/B3, D8-C) requires curated leaf command help to
include a purpose, flags/defaults, at least one runnable example, and related
verbs where useful. The command registry (``commands.json``) is the structured
source for that help content: ``detail`` is the purpose, ``usage`` is the
runnable example with flags, and ``related`` lists related verbs.

These tests assert the rubric for the curated summary verbs and, forward-
lookingly, for the whole Visibility summary surface — so a newly registered
summary command cannot ship without its help metadata.
"""

from __future__ import annotations

import argparse

import pytest

from tokenpak import _cli_core
from tokenpak.cli import _output_contract as oc


@pytest.fixture(scope="module")
def parser():
    return _cli_core.build_parser()


def _subparser(parser, verb):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices.get(verb)
    return None


@pytest.mark.parametrize("verb", oc.JSON_REQUIRED)
def test_curated_summary_help_meets_rubric(parser, verb):
    """Curated summary verbs carry purpose, example+flags, and related verbs."""
    entry = oc.registry_entry(verb)
    assert entry is not None, f"'{verb}' missing from command registry"

    missing = list(oc.missing_rubric_fields(entry))
    assert not missing, f"leaf help for '{verb}' missing rubric field(s): {missing}"

    # Help must actually be wired in the parser (purpose surfaced to the user).
    sub = _subparser(parser, verb)
    assert sub is not None, f"'{verb}' has no built subparser"
    assert sub.format_help().strip(), f"'{verb}' produces empty --help output"


def test_visibility_surface_has_complete_help_metadata():
    """Every Visibility summary command carries complete rubric metadata.

    Forward guard: a new summary command added to the registry without a
    purpose, example, or related verbs fails here.
    """
    offenders: dict[str, list[str]] = {}
    for verb in oc.registry_commands_in_category(oc.SUMMARY_CATEGORY):
        entry = oc.registry_entry(verb) or {}
        missing = list(oc.missing_rubric_fields(entry))
        if missing:
            offenders[verb] = missing
    assert not offenders, f"Visibility commands with incomplete leaf-help metadata: {offenders}"


@pytest.mark.parametrize("verb", oc.JSON_REQUIRED)
def test_curated_usage_advertises_runnable_example(verb):
    """The ``usage`` example is a runnable ``tokenpak`` invocation naming the verb."""
    entry = oc.registry_entry(verb) or {}
    usage = (entry.get("usage") or "").strip()
    assert usage, f"'{verb}' has no usage example"
    assert "tokenpak" in usage, f"'{verb}' usage is not a runnable tokenpak example: {usage!r}"
