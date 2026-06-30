# SPDX-License-Identifier: Apache-2.0
"""Platform-abstraction tests for the proxy watchdog (CP-03/CP-05).

Verifies the watchdog no longer depends on `curl`, `ss`, or `pkill`:
  - health/stats checks use Python HTTP (urllib),
  - port checks use a pure socket probe,
  - restart kills via `pkill` only on POSIX and falls back to PID-file
    termination (no `pkill`) on native Windows,
  - the background proxy is launched via module execution with no `~/tokenpak`
    cwd assumption.
"""

from __future__ import annotations

import socket
import sys
from unittest import mock

import pytest

from tokenpak.platform import process
from tokenpak.proxy import proxy_watchdog
from tokenpak.proxy.proxy_watchdog import ProxyWatchdog


def _force_platform(monkeypatch, label: str) -> None:
    monkeypatch.setattr(process, "current_platform", lambda: label)


def _no_subprocess(monkeypatch):
    """Replace subprocess.run with a tripwire that fails if any binary is shelled out."""
    run = mock.Mock(side_effect=AssertionError("subprocess.run must not be called"))
    monkeypatch.setattr(proxy_watchdog.subprocess, "run", run)
    return run


# --------------------------------------------------------------------------- #
# is_proxy_running — Python HTTP, never curl
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "payload,expected",
    [({"status": "ok"}, True), ({"status": "degraded"}, True), ({"status": "down"}, False), (None, False)],
)
def test_is_proxy_running_uses_http(monkeypatch, payload, expected):
    _no_subprocess(monkeypatch)  # no curl
    monkeypatch.setattr(proxy_watchdog, "_http_get_json", lambda *_a, **_k: payload)
    assert ProxyWatchdog().is_proxy_running() is expected


def test_check_error_rate_uses_http(monkeypatch):
    _no_subprocess(monkeypatch)
    monkeypatch.setattr(proxy_watchdog, "_http_get_json", lambda *_a, **_k: {"errors": 99})
    # Should not raise and should not shell out to curl.
    ProxyWatchdog().check_error_rate()


# --------------------------------------------------------------------------- #
# is_port_listening — pure socket, never `ss`
# --------------------------------------------------------------------------- #


def test_is_port_listening_delegates_to_socket_probe(monkeypatch):
    _no_subprocess(monkeypatch)  # no `ss`
    monkeypatch.setattr(process, "port_in_use", lambda *_a, **_k: True)
    assert ProxyWatchdog().is_port_listening() is True
    monkeypatch.setattr(process, "port_in_use", lambda *_a, **_k: False)
    assert ProxyWatchdog().is_port_listening() is False


# --------------------------------------------------------------------------- #
# restart_proxy — pkill on POSIX, PID-file fallback on Windows
# --------------------------------------------------------------------------- #


def test_restart_windows_uses_pidfile_not_pkill(monkeypatch):
    _force_platform(monkeypatch, "windows")
    terminate = mock.Mock(return_value=True)
    start = mock.Mock()
    monkeypatch.setattr(process, "read_pid_file", lambda *_a, **_k: 4321)
    monkeypatch.setattr(process, "terminate", terminate)
    monkeypatch.setattr(process, "start_background", start)
    monkeypatch.setattr(proxy_watchdog.time, "sleep", lambda *_a, **_k: None)

    wd = ProxyWatchdog()
    monkeypatch.setattr(wd, "is_proxy_running", lambda: True)

    assert wd.restart_proxy() is True
    terminate.assert_called_once_with(4321)  # PID-file fallback used
    start.assert_called_once()
    launched = start.call_args[0][0]
    assert launched == [sys.executable, "-m", "tokenpak.proxy"]
    # No `~/tokenpak` cwd assumption.
    assert "cwd" not in start.call_args.kwargs or start.call_args.kwargs["cwd"] is None


def test_restart_linux_uses_pattern_kill(monkeypatch):
    _force_platform(monkeypatch, "linux")
    kill = mock.Mock(return_value=(True, "pkill"))
    terminate = mock.Mock()
    start = mock.Mock()
    monkeypatch.setattr(process, "kill_by_pattern", kill)
    monkeypatch.setattr(process, "terminate", terminate)
    monkeypatch.setattr(process, "start_background", start)
    monkeypatch.setattr(proxy_watchdog.time, "sleep", lambda *_a, **_k: None)

    wd = ProxyWatchdog()
    monkeypatch.setattr(wd, "is_proxy_running", lambda: True)

    assert wd.restart_proxy() is True
    kill.assert_called_once()
    terminate.assert_not_called()  # pattern kill succeeded; no PID fallback
    start.assert_called_once()


def test_check_memory_usage_skipped_on_windows(monkeypatch):
    _force_platform(monkeypatch, "windows")
    run = _no_subprocess(monkeypatch)  # no pgrep/ps
    ProxyWatchdog().check_memory_usage()
    run.assert_not_called()


# --------------------------------------------------------------------------- #
# process.port_in_use — real socket behavior (the `ss` replacement)
# --------------------------------------------------------------------------- #


def test_port_in_use_real_socket():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert process.port_in_use(port, host="127.0.0.1") is True
    finally:
        srv.close()
    assert process.port_in_use(port, host="127.0.0.1") is False


# --------------------------------------------------------------------------- #
# process.kill_by_pattern — platform dispatch
# --------------------------------------------------------------------------- #


def test_kill_by_pattern_windows_unsupported(monkeypatch):
    _force_platform(monkeypatch, "windows")
    run = mock.Mock(side_effect=AssertionError("must not shell out on windows"))
    monkeypatch.setattr(process.subprocess, "run", run)
    ok, reason = process.kill_by_pattern(["tokenpak.proxy"])
    assert ok is False and "windows" in reason.lower()
    run.assert_not_called()


def test_kill_by_pattern_posix_without_pkill(monkeypatch):
    _force_platform(monkeypatch, "linux")
    monkeypatch.setattr(process.shutil, "which", lambda _: None)
    ok, reason = process.kill_by_pattern(["tokenpak.proxy"])
    assert ok is False and "pkill" in reason


def test_kill_by_pattern_posix_with_pkill(monkeypatch):
    _force_platform(monkeypatch, "linux")
    monkeypatch.setattr(process.shutil, "which", lambda _: "/usr/bin/pkill")
    run = mock.Mock(return_value=mock.Mock(returncode=0))
    monkeypatch.setattr(process.subprocess, "run", run)
    ok, how = process.kill_by_pattern(["tokenpak.proxy", "tokenpak/proxy"])
    assert ok is True and how == "pkill"
    assert run.call_args_list[0][0][0] == ["pkill", "-f", "tokenpak.proxy"]
