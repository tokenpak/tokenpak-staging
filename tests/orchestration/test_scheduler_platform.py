# SPDX-License-Identifier: Apache-2.0
"""Platform-abstraction tests for the macro scheduler (CP-03/CP-05).

The scheduler's crontab/at operations now route through the platform service
adapter. These tests verify that:
  - Linux keeps full crontab/at behavior,
  - native Windows degrades to "no scheduling backend" without invoking
    `crontab`/`at`,
  - the scheduler still records schedules even when the backend is unavailable.
"""

from __future__ import annotations

from unittest import mock

import pytest

from tokenpak.orchestration.macros import scheduler as sched_mod
from tokenpak.orchestration.macros.scheduler import MacroScheduler
from tokenpak.platform import process, service


def _force_platform(monkeypatch, label: str) -> None:
    monkeypatch.setattr(process, "current_platform", lambda: label)


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    # Silence the trusted-user-code notice side effect in scheduling calls.
    monkeypatch.setattr(sched_mod, "_emit_trusted_user_code_notice", lambda *_a, **_k: None)
    return MacroScheduler(schedule_path=tmp_path / "scheduled.json")


# --------------------------------------------------------------------------- #
# service cron/at availability dispatch
# --------------------------------------------------------------------------- #


def test_cron_unsupported_on_windows(monkeypatch):
    _force_platform(monkeypatch, "windows")
    run = mock.Mock()
    monkeypatch.setattr(service.subprocess, "run", run)

    assert service.cron_supported() is False
    assert service.cron_read() is None
    assert service.cron_write(["* * * * * echo hi"]) is False
    assert service.at_submit("now + 1 minute", "echo hi") is False
    run.assert_not_called()  # never invokes crontab/at on Windows


def test_cron_supported_on_linux(monkeypatch):
    _force_platform(monkeypatch, "linux")
    monkeypatch.setattr(service.shutil, "which", lambda _: "/usr/bin/crontab")
    monkeypatch.setattr(
        service.subprocess,
        "run",
        mock.Mock(return_value=mock.Mock(returncode=0, stdout="a\nb\n", stderr="")),
    )
    assert service.cron_supported() is True
    assert service.cron_read() == ["a", "b"]
    assert service.cron_write(["x"]) is True


def test_cron_supported_macos_requires_binary(monkeypatch):
    _force_platform(monkeypatch, "macos")
    monkeypatch.setattr(service.shutil, "which", lambda _: None)
    assert service.cron_supported() is False


# --------------------------------------------------------------------------- #
# MacroScheduler internals route through the adapter
# --------------------------------------------------------------------------- #


def test_crontab_lines_empty_on_windows(scheduler, monkeypatch):
    _force_platform(monkeypatch, "windows")
    run = mock.Mock()
    monkeypatch.setattr(service.subprocess, "run", run)

    assert scheduler._crontab_lines() == []
    assert scheduler._write_crontab(["a"]) is False
    assert scheduler._schedule_at_command("now", "echo") is False
    run.assert_not_called()


def test_crontab_lines_on_linux(scheduler, monkeypatch):
    _force_platform(monkeypatch, "linux")
    monkeypatch.setattr(service.shutil, "which", lambda _: "/usr/bin/crontab")
    monkeypatch.setattr(
        service.subprocess,
        "run",
        mock.Mock(return_value=mock.Mock(returncode=0, stdout="0 9 * * * job\n", stderr="")),
    )
    assert scheduler._crontab_lines() == ["0 9 * * * job"]


def test_schedule_cron_records_even_when_backend_unavailable(scheduler, monkeypatch):
    """A schedule is still persisted on Windows; only the cron wiring no-ops."""
    _force_platform(monkeypatch, "windows")
    monkeypatch.setattr(service.subprocess, "run", mock.Mock())

    record = scheduler.schedule_cron("nightly", "0 0 * * *")
    assert record.name == "nightly"
    assert scheduler.get(record.id) is not None
