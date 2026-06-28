# SPDX-License-Identifier: Apache-2.0
"""Tests for the in-launcher 'update available' nudge (v1.8.0 L1).

All five cases keep CI offline — the PyPI fetch is always mocked
(``live_api_allowed: false``).
"""

import io
import json
import time
from argparse import Namespace
from unittest import mock

import pytest

from tokenpak import _cli_core, _paths


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Route the update-check cache to a throwaway home and clear the opt-out."""
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path))
    monkeypatch.delenv("TOKENPAK_NO_UPDATE_CHECK", raising=False)
    yield


def _seed_cache(checked_at, latest):
    _paths.ensure_home()
    _paths.update_check_cache().write_text(
        json.dumps({"checked_at": checked_at, "latest": latest})
    )


# 1. nudge IS shown when a newer PyPI version exists --------------------------
def test_nudge_shown_when_newer(monkeypatch):
    monkeypatch.setattr("tokenpak.__version__", "1.0.0")
    monkeypatch.setattr(_cli_core, "_fetch_latest_pypi_version", lambda timeout=5.0: "2.0.0")
    buf = io.StringIO()

    _cli_core._maybe_update_nudge(stream=buf)

    out = buf.getvalue()
    assert "2.0.0" in out
    assert "available" in out
    assert "tokenpak update" in out


# 2. offline / network failure does NOT block the launcher (fail-open) --------
def test_failopen_on_network_error(monkeypatch):
    monkeypatch.setattr("tokenpak.__version__", "1.0.0")

    def _boom(timeout=5.0):
        raise OSError("network down")

    monkeypatch.setattr(_cli_core, "_fetch_latest_pypi_version", _boom)
    buf = io.StringIO()

    # Must not raise, must return None, must print nothing.
    assert _cli_core._maybe_update_nudge(stream=buf) is None
    assert buf.getvalue() == ""
    # Failure is still cached (latest=None) so we don't retry until tomorrow.
    checked_at, latest = _cli_core._read_update_cache()
    assert latest is None
    assert checked_at > 0


# 3. once-per-day cache: no call when fresh, refresh when stale ---------------
def test_cache_skips_network_when_fresh(monkeypatch):
    monkeypatch.setattr("tokenpak.__version__", "1.0.0")
    _seed_cache(checked_at=time.time(), latest="2.0.0")
    fetch = mock.Mock(return_value="9.9.9")
    monkeypatch.setattr(_cli_core, "_fetch_latest_pypi_version", fetch)
    buf = io.StringIO()

    _cli_core._maybe_update_nudge(stream=buf)

    fetch.assert_not_called()
    assert "2.0.0" in buf.getvalue()  # served from cache, not the network


def test_cache_refreshes_when_stale(monkeypatch):
    monkeypatch.setattr("tokenpak.__version__", "1.0.0")
    stale = time.time() - (25 * 60 * 60)
    _seed_cache(checked_at=stale, latest=None)
    fetch = mock.Mock(return_value="2.0.0")
    monkeypatch.setattr(_cli_core, "_fetch_latest_pypi_version", fetch)
    buf = io.StringIO()

    _cli_core._maybe_update_nudge(stream=buf)

    fetch.assert_called_once()
    assert "2.0.0" in buf.getvalue()
    checked_at, latest = _cli_core._read_update_cache()
    assert latest == "2.0.0"
    assert checked_at > stale  # cache timestamp advanced


# 4. opt-out via TOKENPAK_NO_UPDATE_CHECK=1 ----------------------------------
def test_optout_suppresses_check(monkeypatch):
    monkeypatch.setenv("TOKENPAK_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr("tokenpak.__version__", "1.0.0")
    fetch = mock.Mock(return_value="2.0.0")
    monkeypatch.setattr(_cli_core, "_fetch_latest_pypi_version", fetch)
    buf = io.StringIO()

    _cli_core._maybe_update_nudge(stream=buf)

    fetch.assert_not_called()
    assert buf.getvalue() == ""


# 5. only the claude/codex launchers nudge — never other verbs ----------------
def test_only_launchers_nudge(monkeypatch):
    nudge = mock.Mock()
    monkeypatch.setattr(_cli_core, "_maybe_update_nudge", nudge)

    # claude launcher: nudges then launches.
    monkeypatch.setattr("tokenpak.companion.launch", mock.Mock(), raising=False)
    _cli_core.cmd_claude(Namespace(args=[], budget=None))
    assert nudge.call_count == 1

    # codex launcher: nudges then launches.
    monkeypatch.setattr("tokenpak.companion.codex.launch", mock.Mock(), raising=False)
    _cli_core.cmd_codex(Namespace(args=[], budget=None, install_only=False))
    assert nudge.call_count == 2

    # A non-launcher verb (`update`) must NOT fire the nudge.
    monkeypatch.setattr(_cli_core, "_fetch_latest_pypi_version", lambda timeout=5.0: "2.0.0")
    monkeypatch.setattr("tokenpak.__version__", "2.0.0")
    _cli_core.cmd_update(
        Namespace(check=True, force=False, core_only=False, dry_run=False)
    )
    assert nudge.call_count == 2  # unchanged — update did not nudge


def test_claude_launcher_prints_orientation_and_home_tip(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_cli_core, "_maybe_update_nudge", lambda: None)
    launch = mock.Mock()
    monkeypatch.setattr("tokenpak.companion.launch", launch, raising=False)

    _cli_core.cmd_claude(Namespace(args=[], budget=None))

    out = capsys.readouterr().out
    assert "Launching Claude Code through TokenPak." in out
    assert "trust this folder" in out
    assert "launch from your project directory" in out
    launch.assert_called_once_with(args=[])
