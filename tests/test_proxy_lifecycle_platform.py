# SPDX-License-Identifier: Apache-2.0
"""Platform-abstraction tests for proxy lifecycle in `_cli_core` (CP-03/CP-05).

Covers stale-PID handling and detached background start across simulated Linux,
macOS, and native Windows, plus the underlying `tokenpak.platform.process`
primitives:
  - `pid_alive` is a true liveness probe on POSIX and never calls
    `os.kill(pid, 0)` on Windows (where that would terminate the process),
  - `terminate` signals correctly per platform,
  - `start_background` uses `start_new_session` on POSIX and detached creation
    flags on Windows.
"""

from __future__ import annotations

import signal
import sys
import types
from unittest import mock

import pytest

import tokenpak._cli_core as cli_core
from tokenpak.platform import process


def _force_platform(monkeypatch, label: str) -> None:
    monkeypatch.setattr(process, "current_platform", lambda: label)


# --------------------------------------------------------------------------- #
# process.start_background — detach semantics per platform
# --------------------------------------------------------------------------- #


def test_start_background_posix_uses_new_session(monkeypatch):
    _force_platform(monkeypatch, "linux")
    popen = mock.Mock()
    monkeypatch.setattr(process.subprocess, "Popen", popen)

    process.start_background(["echo", "hi"], cwd="/tmp", env={"X": "1"})

    kwargs = popen.call_args.kwargs
    assert kwargs["start_new_session"] is True
    assert "creationflags" not in kwargs


def test_start_background_windows_uses_creationflags(monkeypatch):
    _force_platform(monkeypatch, "windows")
    popen = mock.Mock()
    monkeypatch.setattr(process.subprocess, "Popen", popen)

    process.start_background(["proxy.exe"])

    kwargs = popen.call_args.kwargs
    assert kwargs["creationflags"] == (
        process._DETACHED_PROCESS | process._CREATE_NEW_PROCESS_GROUP
    )
    assert "start_new_session" not in kwargs


# --------------------------------------------------------------------------- #
# process.pid_alive — liveness probe; Windows must NOT os.kill
# --------------------------------------------------------------------------- #


def test_pid_alive_posix_true(monkeypatch):
    _force_platform(monkeypatch, "linux")
    killer = mock.Mock()
    monkeypatch.setattr(process.os, "kill", killer)
    assert process.pid_alive(123) is True
    killer.assert_called_once_with(123, 0)


def test_pid_alive_posix_dead(monkeypatch):
    _force_platform(monkeypatch, "linux")
    monkeypatch.setattr(process.os, "kill", mock.Mock(side_effect=ProcessLookupError))
    assert process.pid_alive(123) is False


def test_pid_alive_posix_permission_means_exists(monkeypatch):
    _force_platform(monkeypatch, "linux")
    monkeypatch.setattr(process.os, "kill", mock.Mock(side_effect=PermissionError))
    assert process.pid_alive(123) is True


def test_pid_alive_windows_does_not_os_kill(monkeypatch):
    _force_platform(monkeypatch, "windows")
    tripwire = mock.Mock(side_effect=AssertionError("os.kill must not run on Windows liveness"))
    monkeypatch.setattr(process.os, "kill", tripwire)
    monkeypatch.setattr(process, "_pid_alive_windows", lambda pid: True)
    assert process.pid_alive(999) is True
    tripwire.assert_not_called()


def test_pid_alive_rejects_nonpositive():
    assert process.pid_alive(0) is False
    assert process.pid_alive(-5) is False


# --------------------------------------------------------------------------- #
# process.terminate — platform-correct signalling
# --------------------------------------------------------------------------- #


def test_terminate_posix_sigterm(monkeypatch):
    _force_platform(monkeypatch, "linux")
    killer = mock.Mock()
    monkeypatch.setattr(process.os, "kill", killer)
    assert process.terminate(321) is True
    killer.assert_called_once_with(321, signal.SIGTERM)


def test_terminate_posix_force_sigkill(monkeypatch):
    _force_platform(monkeypatch, "linux")
    killer = mock.Mock()
    monkeypatch.setattr(process.os, "kill", killer)
    assert process.terminate(321, force=True) is True
    killer.assert_called_once_with(321, signal.SIGKILL)


def test_terminate_already_gone_is_success(monkeypatch):
    _force_platform(monkeypatch, "linux")
    monkeypatch.setattr(process.os, "kill", mock.Mock(side_effect=ProcessLookupError))
    assert process.terminate(321) is True


def test_terminate_error_returns_false(monkeypatch):
    _force_platform(monkeypatch, "linux")
    monkeypatch.setattr(process.os, "kill", mock.Mock(side_effect=PermissionError))
    assert process.terminate(321) is False


def test_terminate_windows_uses_os_kill_sigterm(monkeypatch):
    _force_platform(monkeypatch, "windows")
    killer = mock.Mock()
    monkeypatch.setattr(process.os, "kill", killer)
    assert process.terminate(321) is True
    killer.assert_called_once_with(321, signal.SIGTERM)


# --------------------------------------------------------------------------- #
# cmd_stop — stale-PID handling across platforms
# --------------------------------------------------------------------------- #


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    pid_path = tmp_path / ".tokenpak" / "proxy.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    return pid_path


def test_cmd_stop_no_pidfile_returns_1(home):
    assert cli_core.cmd_stop(types.SimpleNamespace()) == 1


@pytest.mark.parametrize("plat", ["linux", "macos", "windows"])
def test_cmd_stop_stale_pid_removed(home, monkeypatch, plat):
    _force_platform(monkeypatch, plat)
    home.write_text("12345")
    monkeypatch.setattr(process, "pid_alive", lambda *_a, **_k: False)
    assert cli_core.cmd_stop(types.SimpleNamespace()) is None
    assert not home.exists()  # stale PID file cleaned up


@pytest.mark.parametrize("plat", ["linux", "macos", "windows"])
def test_cmd_stop_alive_terminates(home, monkeypatch, plat):
    _force_platform(monkeypatch, plat)
    home.write_text("12345")
    monkeypatch.setattr(process, "pid_alive", lambda *_a, **_k: True)
    terminate = mock.Mock(return_value=True)
    monkeypatch.setattr(process, "terminate", terminate)
    assert cli_core.cmd_stop(types.SimpleNamespace()) is None
    terminate.assert_called_once_with(12345)
    assert not home.exists()


def test_cmd_stop_terminate_failure_returns_1(home, monkeypatch):
    _force_platform(monkeypatch, "linux")
    home.write_text("12345")
    monkeypatch.setattr(process, "pid_alive", lambda *_a, **_k: True)
    monkeypatch.setattr(process, "terminate", lambda *_a, **_k: False)
    assert cli_core.cmd_stop(types.SimpleNamespace()) == 1
    assert home.exists()  # PID file left in place on failure


def test_cmd_stop_invalid_pid_returns_1(home):
    home.write_text("not-a-pid")
    assert cli_core.cmd_stop(types.SimpleNamespace()) == 1


# --------------------------------------------------------------------------- #
# cmd_start — alive PID short-circuits before launching
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("plat", ["linux", "macos", "windows"])
def test_cmd_start_existing_pid_does_not_relaunch(home, monkeypatch, plat):
    _force_platform(monkeypatch, plat)
    home.write_text("777")

    # Not already responding on the port.
    monkeypatch.setattr(cli_core, "_proxy_get", lambda *_a, **_k: None)
    # Config validation is irrelevant here — make it a no-op pass.
    monkeypatch.setattr(
        "tokenpak.core.config_loader.load_config", lambda *_a, **_k: {}, raising=False
    )
    monkeypatch.setattr(
        "tokenpak.core.config_validator.ConfigValidator.validate",
        lambda self, cfg: [],
        raising=False,
    )
    # The PID is alive → must short-circuit, never launch.
    monkeypatch.setattr(process, "pid_alive", lambda *_a, **_k: True)
    start = mock.Mock(side_effect=AssertionError("must not launch when PID alive"))
    monkeypatch.setattr(process, "start_background", start)

    args = types.SimpleNamespace(port=8766, log_level=None, config=None)
    assert cli_core.cmd_start(args) == 1
    start.assert_not_called()
