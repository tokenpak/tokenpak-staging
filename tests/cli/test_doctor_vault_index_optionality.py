# SPDX-License-Identifier: Apache-2.0
"""Doctor copy contract for optional vault indexing."""

from __future__ import annotations

import importlib.util
import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch


def _load_doctor_module():
    module_path = (
        Path(__file__).resolve().parents[2] / "tokenpak/cli/commands/doctor.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_tokenpak_doctor_vault_index_test", module_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_doctor_json(monkeypatch, tmp_path, health):
    doctor_mod = _load_doctor_module()
    fake_home = tmp_path / "home"
    fake_tpk = fake_home / ".tpk"
    fake_tpk.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("TOKENPAK_HOME", str(fake_tpk))

    captured = StringIO()
    with (
        patch.object(doctor_mod, "_proxy_get", return_value=health),
        patch("sys.stdout", captured),
    ):
        doctor_mod.run_doctor(output_json=True)

    return json.loads(captured.getvalue())


def _vault_index_message(payload: dict) -> str:
    for check in payload["checks"]:
        if check["check"] == "vault_index":
            return check["message"]
    raise AssertionError("vault_index check missing from doctor output")


def test_missing_vault_index_says_optional_and_enables_semantic_search(
    monkeypatch, tmp_path
):
    payload = _run_doctor_json(monkeypatch, tmp_path, health=None)

    message = _vault_index_message(payload)

    assert "not found" in message
    assert "optional" in message
    assert "tokenpak index <path>" in message
    assert "semantic search" in message


def test_unavailable_proxy_vault_index_says_optional_and_actionable(
    monkeypatch, tmp_path
):
    health = {"vault_index": {"available": False, "blocks": 0}}

    payload = _run_doctor_json(monkeypatch, tmp_path, health=health)

    message = _vault_index_message(payload)

    assert "not available" in message
    assert "optional" in message
    assert "tokenpak index <path>" in message
    assert "semantic search" in message
