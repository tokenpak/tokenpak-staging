# SPDX-License-Identifier: Apache-2.0
"""CLI version honesty regression guards.

These tests pin the honesty invariants for the in-scope CLI surface:

* ``tokenpak version`` must not advertise a stale hardcoded proxy version. The
  proxy ships from the same wheel as the CLI, so the "expected" proxy version is
  derived from ``tokenpak.__version__`` rather than a separate literal (which had
  drifted to ``"1.1.0"``: a version the package never shipped).
* The advertised connector registry must exclude connectors that only raise
  ``NotImplementedError``; stubs are documented as future work, never registered
  as a working surface.
"""
from __future__ import annotations

import inspect
from argparse import Namespace

import tokenpak
from tokenpak import _cli_core


def test_expected_proxy_version_tracks_package_version():
    # Semantic guard: the expected proxy version is the installed package
    # version, not a separate constant that can silently drift out of sync.
    assert _cli_core.PROXY_VERSION == tokenpak.__version__


def test_no_stale_hardcoded_proxy_version_literal():
    # Structural guard for the exact residue removed here: the module
    # used to pin ``PROXY_VERSION = "1.1.0"``, advertised by ``tokenpak version``
    # as the expected proxy version.
    src = inspect.getsource(_cli_core)
    assert 'PROXY_VERSION = "1.1.0"' not in src


def test_cmd_version_reports_honest_expected_proxy(monkeypatch, capsys):
    # Behavioral guard: the rendered ``Proxy (expected)`` line matches the
    # package version and never the stale literal. Proxy reachability is stubbed
    # so the test is hermetic and offline.
    monkeypatch.setattr(_cli_core, "_get_proxy_version", lambda: {"error": "offline"})
    _cli_core.cmd_version(Namespace())
    out = capsys.readouterr().out
    assert f"Proxy (expected) : {tokenpak.__version__}" in out
    assert "Proxy (expected) : 1.1.0" not in out


def test_advertised_connectors_exclude_notimplemented_stubs():
    # The advertised connector registry must list only working connectors.
    # notion/google_drive/github raise NotImplementedError and must not appear.
    sources = __import__("tokenpak.sources", fromlist=[""])
    advertised = set(sources.list_connectors())
    for stub in ("notion", "google_drive", "github"):
        assert stub not in advertised
