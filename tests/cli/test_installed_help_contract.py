# SPDX-License-Identifier: Apache-2.0
"""Installed CLI help-tier contract regressions."""

from __future__ import annotations

import re
from contextlib import redirect_stdout
from io import StringIO

from tokenpak._cli_core import _print_quick_help
from tokenpak.cli.commands.help import (
    print_command_help,
    print_essential_help,
    print_full_help,
)

COMMAND_ROW = re.compile(r"^\s{2,}([a-z][a-z0-9-]+)\s{2,}")
CANONICAL_FIRST_RUN = {
    "serve",
    "integrate",
    "cost",
    "savings",
    "demo",
    "doctor",
    "status",
    "creds",
    "cache",
    "index",
    "replay",
}


def _capture(fn, *args) -> str:
    buf = StringIO()
    with redirect_stdout(buf):
        fn(*args)
    return buf.getvalue()


def _command_rows(text: str) -> set[str]:
    return {
        match.group(1)
        for line in text.splitlines()
        if (match := COMMAND_ROW.match(line))
    }


def test_help_tiers_strictly_nest_and_surface_first_run_verbs() -> None:
    quick = _command_rows(_capture(_print_quick_help))
    common = _command_rows(_capture(print_essential_help))
    all_commands = _command_rows(_capture(print_full_help))

    assert quick
    assert quick <= common
    assert common <= all_commands
    assert CANONICAL_FIRST_RUN <= common
    assert CANONICAL_FIRST_RUN <= all_commands


def test_help_command_resolves_registry_command_integrate() -> None:
    output = _capture(print_command_help, "integrate")

    assert "tokenpak integrate" in output
    assert "Client-specific setup guides" in output
