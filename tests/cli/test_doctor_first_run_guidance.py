# SPDX-License-Identifier: Apache-2.0
"""Focused doctor copy contracts for first-run guidance."""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import patch

from tokenpak.cli.commands import doctor


def test_lifecycle_summary_uses_parser_real_restart_command():
    out = doctor.build_lifecycle_summary(
        version="1.0.0",
        setup_present=True,
        route_state="active",
        proxy_state="stopped",
        update_state="current",
        update_latest=None,
    )
    assert "Run: tokenpak restart" in out
    assert "tokenpak proxy " + "restart" not in out


def test_lifecycle_summary_reports_running_proxy_version_separately():
    out = doctor.build_lifecycle_summary(
        version="1.9.0",
        setup_present=True,
        route_state="active",
        proxy_state="running",
        update_state="current",
        update_latest=None,
        running_proxy_version="1.9.0",
    )
    assert "Installed package" in out
    assert "Running proxy" in out
    assert "v1.9.0 matches installed package" not in out


def test_lifecycle_summary_warns_on_unacknowledged_runtime_skew():
    out = doctor.build_lifecycle_summary(
        version="1.9.0",
        setup_present=True,
        route_state="active",
        proxy_state="running",
        update_state="current",
        update_latest=None,
        running_proxy_version="1.7.1",
    )
    assert "v1.7.1 != package v1.9.0" in out
    assert "Run: tokenpak restart" in out


def test_lifecycle_summary_can_label_acknowledged_runtime_skew():
    out = doctor.build_lifecycle_summary(
        version="1.9.0",
        setup_present=True,
        route_state="active",
        proxy_state="running",
        update_state="current",
        update_latest=None,
        running_proxy_version="1.7.1",
        runtime_version_acknowledged=True,
    )
    assert "v1.7.1 (acknowledged)" in out
    assert "package v1.9.0" in out
    assert "Run: tokenpak restart" not in out


def test_api_key_setup_detail_includes_windows_and_posix_examples():
    detail = doctor._api_key_setup_detail()
    assert "no direct API key" in detail
    assert "export ANTHROPIC_API_KEY=sk-..." in detail
    assert 'setx ANTHROPIC_API_KEY "sk-..."' in detail
    assert "set ANTHROPIC_API_KEY=sk-..." in detail


class _ClosedSocket:
    def settimeout(self, _timeout: float) -> None:
        return None

    def connect_ex(self, _addr: tuple[str, int]) -> int:
        return 111

    def close(self) -> None:
        return None


def _doctor_json(monkeypatch, tmp_path) -> dict:
    fake_home = tmp_path / "home"
    tokenpak_home = fake_home / ".tpk"
    tokenpak_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("TOKENPAK_HOME", str(tokenpak_home))
    monkeypatch.setattr(doctor, "_route_state", lambda: ("not routed", None))
    monkeypatch.setattr(doctor, "_update_state", lambda: ("unknown", None))
    monkeypatch.setattr(doctor, "_proxy_state", lambda: "stopped")
    monkeypatch.setattr(doctor, "_proxy_get", lambda *_a, **_k: None)
    monkeypatch.setattr(doctor.socket, "socket", lambda *_a, **_k: _ClosedSocket())
    monkeypatch.setattr("tokenpak.creds.doctor.run", lambda *a, **k: [])

    captured = StringIO()
    with patch("sys.stdout", captured):
        doctor.run_doctor(output_json=True)
    text = captured.getvalue()
    start = text.rfind("\n{")
    return json.loads(text[start + 1 :] if start != -1 else text)


def _find_check(payload: dict, name: str) -> dict:
    for check in payload["checks"]:
        if check["check"] == name:
            return check
    raise AssertionError(f"{name} check missing")


def test_disk_usage_probe_stops_at_entry_limit(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    for i in range(5):
        (root / f"file-{i}.txt").write_text("x")

    result = doctor._measure_disk_usage(root, max_entries=3, timeout_s=60.0)

    assert result.truncated is True
    assert result.reason == "entry limit 3"
    assert result.entries == 3


def test_doctor_reports_bounded_disk_usage_as_warning(monkeypatch, tmp_path):
    monkeypatch.setattr(
        doctor,
        "_measure_disk_usage",
        lambda *_a, **_k: doctor._DiskUsageResult(
            total_bytes=2048,
            files=2,
            entries=7,
            truncated=True,
            reason="timeout 0.25s",
        ),
    )

    check = _find_check(_doctor_json(monkeypatch, tmp_path), "disk_usage")

    assert check["status"] == "warn"
    assert "bounded after 7 entries" in check["message"]
    assert "timeout 0.25s" in check["message"]
    assert "tokenpak maintenance" in check["message"]
