# SPDX-License-Identifier: Apache-2.0
"""Regression coverage for source distribution member pruning."""

from __future__ import annotations

import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _archive_members(archive: Path) -> list[PurePosixPath]:
    with tarfile.open(archive, mode="r:gz") as tar:
        members = []
        for name in tar.getnames():
            path = PurePosixPath(name)
            members.append(PurePosixPath(*path.parts[1:]))
        return members


@pytest.mark.timeout(120)
def test_sdist_prunes_top_level_tests(tmp_path):
    outdir = tmp_path / "dist"
    result = subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--outdir", str(outdir)],
        cwd=_REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    archives = list(outdir.glob("*.tar.gz"))
    assert len(archives) == 1

    members = _archive_members(archives[0])
    top_level_tests = [str(path) for path in members if path.parts and path.parts[0] == "tests"]
    internal_tests = [
        str(path)
        for path in members
        if len(path.parts) >= 2 and path.parts[:2] == ("tests", "_internal")
    ]

    assert top_level_tests == []
    assert internal_tests == []
