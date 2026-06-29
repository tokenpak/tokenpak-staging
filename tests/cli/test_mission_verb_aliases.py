# SPDX-License-Identifier: Apache-2.0
"""Mission-verb aliases route to existing CLI implementations."""
from __future__ import annotations

from tokenpak._cli_core import (
    _MISSION_VERB_ALIASES,
    _rewrite_mission_verb_alias,
    build_parser,
    registered_command_names,
)


def test_mission_aliases_are_registered_top_level_verbs():
    """Parser registration keeps mission verbs from falling into typo hints."""
    registered = registered_command_names(build_parser())
    assert set(_MISSION_VERB_ALIASES) <= registered


def test_mission_aliases_rewrite_to_existing_command_paths():
    """The aliases are thin routes, not new behavioral surfaces."""
    for alias, target in _MISSION_VERB_ALIASES.items():
        assert _rewrite_mission_verb_alias(["tokenpak", alias, "--help"]) == [
            "tokenpak",
            *target,
            "--help",
        ]


def test_mission_alias_rewrite_preserves_global_options():
    """Global parser options before the command should not bypass alias routing."""
    assert _rewrite_mission_verb_alias(
        ["tokenpak", "--db", "registry.db", "pack", "--help"]
    ) == ["tokenpak", "--db", "registry.db", "compress", "--help"]
    assert _rewrite_mission_verb_alias(["tokenpak", "--db=registry.db", "verify"]) == [
        "tokenpak",
        "--db=registry.db",
        "prove",
    ]
