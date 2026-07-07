# SPDX-License-Identifier: Apache-2.0
"""Wheel-truth for the served dashboard shell.

Same defect class as the ``commands.json`` packaging fix (see
``test_command_registry_package_data.py``): the dashboard server serves a set of
static files from ``tokenpak/dashboard/``, but the built wheel shipped only
``dashboard/templates/**/*`` — so a fresh ``pip install`` served ``/dashboard``
as a 404. These tests fail if any file the server serves is not declared as
package data (won't be in the wheel) or is not loadable as a package resource.

The tests are offline and deterministic: they assert the packaging *contract*
(package-data globs + ``importlib.resources`` loadability) rather than building a
wheel, matching the established pattern in this directory.
"""

from __future__ import annotations

import fnmatch
from importlib import resources
from pathlib import Path

from tokenpak.dashboard import get_dashboard_files

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python <3.11
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[2]


def _package_data_globs() -> list[str]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["tool"]["setuptools"]["package-data"]["tokenpak"]


def _served_relpaths() -> dict[str, str]:
    """Map each served file name -> its path relative to the tokenpak package."""
    served = {}
    for name in get_dashboard_files():
        served[name] = f"dashboard/{name}"
    return served


def test_dashboard_static_globs_declared() -> None:
    globs = _package_data_globs()
    for glob in ("dashboard/*.html", "dashboard/*.js", "dashboard/*.css"):
        assert glob in globs, f"missing package-data glob: {glob}"


def test_every_served_file_is_covered_by_package_data() -> None:
    globs = _package_data_globs()
    for name, relpath in _served_relpaths().items():
        assert any(fnmatch.fnmatch(relpath, glob) for glob in globs), (
            f"served dashboard file {name!r} ({relpath}) is not matched by any "
            f"package-data glob and would be missing from the wheel"
        )


def test_every_served_file_loads_as_package_resource() -> None:
    root = resources.files("tokenpak.dashboard")
    for name in get_dashboard_files():
        resource = root.joinpath(name)
        assert resource.is_file(), f"served dashboard file not shippable: {name}"


def test_sessions_js_is_served_and_packaged() -> None:
    """Regression anchor: sessions.js is the only script index.html loads."""
    served = get_dashboard_files()
    assert "sessions.js" in served, "sessions.js must be in the serve whitelist"

    globs = _package_data_globs()
    assert any(fnmatch.fnmatch("dashboard/sessions.js", glob) for glob in globs)
    assert resources.files("tokenpak.dashboard").joinpath("sessions.js").is_file()
