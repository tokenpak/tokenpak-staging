# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import subprocess
import sys


def _run_tokenpak(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tokenpak", *args],
        capture_output=True,
        check=False,
        text=True,
    )


def test_unknown_command_help_flag_exits_nonzero() -> None:
    result = _run_tokenpak("zzz-fake", "--help")

    assert result.returncode != 0
    assert "Unknown command" in result.stderr
    assert "no additional help available" not in result.stdout


def test_known_command_help_still_exits_zero() -> None:
    result = _run_tokenpak("status", "--help")

    assert result.returncode == 0
    assert "usage: tokenpak status" in result.stdout
    assert "Unknown command" not in result.stderr
