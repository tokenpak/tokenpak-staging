# SPDX-License-Identifier: Apache-2.0
"""`tokenpak doctor` must read the unified /health contract and surface Std 03
§9.3 first-run + credential-mode state honestly.

Pins the CLI reader side of the tester-readiness fix:
  * a healthy proxy readout shows real mode / request count / latency with no
    "unknown mode", "0 reqs", or "no latency data" schema-drift artifacts —
    whether the proxy emits the nested (sync) or flat (async) shape;
  * doctor emits the §9.3 first-run sentinel + active credential-mode lines;
  * the Mode-1 OAuth default never triggers a "set ANTHROPIC_API_KEY" warning.
"""

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


def _run_doctor_checks(monkeypatch, tmp_path, *, health: dict | None) -> dict:
    """Run doctor in JSON mode against a stubbed /health; return {name: check}."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("TOKENPAK_HOME", str(fake_home / ".tpk"))
    monkeypatch.setattr(tokenpak, "__version__", "1.7.1")
    monkeypatch.setattr(doctor, "_route_state", lambda: ("not routed", None))
    monkeypatch.setattr(doctor, "_update_state", lambda: ("unknown", None))
    monkeypatch.setattr(
        doctor, "_proxy_state", lambda: "running" if health is not None else "stopped"
    )
    monkeypatch.setattr(doctor, "_proxy_get", lambda *_a, **_k: health)
    monkeypatch.setattr(doctor.socket, "socket", lambda *_a, **_k: _ClosedSocket())

    captured = StringIO()
    with patch("sys.stdout", captured):
        doctor.run_doctor(output_json=True)
    text = captured.getvalue()
    start = text.rfind("\n{")
    payload = json.loads(text[start + 1 :] if start != -1 else text)
    return {c["check"]: c for c in payload["checks"]}


def _nested_health(version: str = "1.7.1") -> dict:
    """Contract-shaped /health (sync emitter shape: nested stats + latency)."""
    return {
        "status": "ok",
        "version": version,
        "uptime_seconds": 120,
        "compilation_mode": "hybrid",
        "requests_total": 9,
        "python_version": "3.12.3",
        "stats": {"requests": 9, "input_tokens": 100},
        "latency": {"p50_latency_ms": 25, "p99_latency_ms": 60, "samples": 9},
    }


def _flat_only_health(version: str = "1.7.1") -> dict:
    """A legacy/partial shape carrying only the flat requests_total + mode.

    Exercises the robust reader fallback so a healthy proxy never reads "0 reqs"
    even if the nested stats block is absent.
    """
    return {
        "status": "ok",
        "version": version,
        "uptime_seconds": 120,
        "compilation_mode": "hybrid",
        "requests_total": 9,
        "latency": {"p50_latency_ms": 25, "p99_latency_ms": 60, "samples": 9},
    }


def test_healthy_proxy_shows_real_mode_reqs_latency(monkeypatch, tmp_path):
    checks = _run_doctor_checks(monkeypatch, tmp_path, health=_nested_health())
    msg = checks["proxy_health"]["message"]
    assert "hybrid mode" in msg
    assert "9 reqs" in msg
    assert "unknown mode" not in msg
    assert "0 reqs" not in msg
    assert "no latency data" not in msg
    assert "p50=" in msg


def test_flat_only_requests_total_fallback(monkeypatch, tmp_path):
    # No nested stats block — reader must fall back to flat requests_total.
    checks = _run_doctor_checks(monkeypatch, tmp_path, health=_flat_only_health())
    msg = checks["proxy_health"]["message"]
    assert "9 reqs" in msg
    assert "0 reqs" not in msg
    assert "hybrid mode" in msg


def test_runtime_version_read_from_contract(monkeypatch, tmp_path):
    # version present in the contract → no "version not reported" drift.
    checks = _run_doctor_checks(monkeypatch, tmp_path, health=_nested_health("1.7.1"))
    rv = checks["runtime_version"]
    assert rv["status"] == "pass"
    assert "not reported" not in rv["message"]


def test_first_run_state_observability_line(monkeypatch, tmp_path):
    checks = _run_doctor_checks(monkeypatch, tmp_path, health=_nested_health())
    assert "first_run_state" in checks
    fr = checks["first_run_state"]
    assert fr["status"] == "pass"
    # fresh home → sentinel absent + canonical home reported.
    assert "absent" in fr["message"]
    assert "canonical" in fr["message"]


def test_credential_mode_oauth_is_healthy_no_key_warning(monkeypatch, tmp_path):
    # OAuth/session path available, no provider API key set.
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", "oauth-sess-token")
    checks = _run_doctor_checks(monkeypatch, tmp_path, health=_nested_health())

    cred = checks["credential_mode"]
    assert cred["status"] == "pass"
    assert "Mode 1" in cred["message"]

    # The api_keys check must not warn / recommend setting a direct key.
    api = checks["api_keys"]
    assert api["status"] == "pass"
    assert "set ANTHROPIC_API_KEY" not in api["message"]


def test_active_credential_mode_classification(monkeypatch):
    # Mode 3 — env var only (no OAuth env, discovery yields nothing usable).
    for var in ("ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN2"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(
        "tokenpak.creds.providers.discover_all", lambda: [], raising=True
    )
    label, _ = doctor._active_credential_mode()
    assert label.startswith("Mode 3")

    # Mode 1 — OAuth env var takes precedence.
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", "oauth-tok")
    label, _ = doctor._active_credential_mode()
    assert label.startswith("Mode 1")
