# SPDX-License-Identifier: Apache-2.0
"""Doctor installed-package vs running-proxy version checks."""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import patch

import tokenpak
from tokenpak.cli.commands import doctor


class _ClosedSocket:
    def settimeout(self, _timeout: float) -> None:
        return None

    def connect_ex(self, _addr: tuple[str, int]) -> int:
        return 111

    def close(self) -> None:
        return None


def _health(version: str, **extra: object) -> dict:
    payload = {
        "version": version,
        "compilation_mode": "normal",
        "latency": {},
        "stats": {},
    }
    payload.update(extra)
    return payload


def _doctor_json(monkeypatch, tmp_path, *, installed: str, health: dict | None) -> dict:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("TOKENPAK_HOME", str(fake_home / ".tpk"))
    monkeypatch.setattr(tokenpak, "__version__", installed)
    monkeypatch.setattr(doctor, "_route_state", lambda: ("not routed", None))
    monkeypatch.setattr(doctor, "_update_state", lambda: ("unknown", None))
    monkeypatch.setattr(
        doctor,
        "_proxy_state",
        lambda: "running" if health is not None else "stopped",
    )
    monkeypatch.setattr(doctor, "_proxy_get", lambda *_a, **_k: health)
    monkeypatch.setattr(doctor.socket, "socket", lambda *_a, **_k: _ClosedSocket())

    captured = StringIO()
    with patch("sys.stdout", captured):
        doctor.run_doctor(output_json=True)
    text = captured.getvalue()
    start = text.rfind("\n{")
    return json.loads(text[start + 1 :] if start != -1 else text)


def _runtime_check(payload: dict) -> dict:
    for check in payload["checks"]:
        if check["check"] == "runtime_version":
            return check
    raise AssertionError("runtime_version check missing")


def test_doctor_runtime_version_passes_when_running_proxy_matches_package(
    monkeypatch, tmp_path
):
    payload = _doctor_json(monkeypatch, tmp_path, installed="1.9.0", health=_health("1.9.0"))
    check = _runtime_check(payload)

    assert check["status"] == "pass"
    assert "matches installed package v1.9.0" in check["message"]


def test_doctor_runtime_version_warns_when_running_proxy_is_stale(monkeypatch, tmp_path):
    payload = _doctor_json(monkeypatch, tmp_path, installed="1.9.0", health=_health("1.7.1"))
    check = _runtime_check(payload)

    assert check["status"] == "warn"
    assert "v1.7.1 differs from installed package v1.9.0" in check["message"]
    assert "restart to adopt" in check["message"]
    assert "remediation=tokenpak restart" in check["detail"]


def test_doctor_runtime_version_can_represent_acknowledged_skew(monkeypatch, tmp_path):
    payload = _doctor_json(
        monkeypatch,
        tmp_path,
        installed="1.9.0",
        health=_health(
            "1.7.1",
            runtime_version_acknowledged=True,
            runtime_version_note="pinned during rollout",
        ),
    )
    check = _runtime_check(payload)

    assert check["status"] == "warn"
    assert "(acknowledged)" in check["message"]
    assert "pinned during rollout" in check["message"]
    assert "remediation=tokenpak restart" not in check["detail"]


def test_doctor_runtime_version_warns_unknown_when_proxy_unreachable(monkeypatch, tmp_path):
    payload = _doctor_json(monkeypatch, tmp_path, installed="1.9.0", health=None)
    check = _runtime_check(payload)

    assert check["status"] == "warn"
    assert "version unknown" in check["message"]
    assert "running_proxy=unknown" in check["detail"]
