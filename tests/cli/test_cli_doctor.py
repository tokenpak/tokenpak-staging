# SPDX-License-Identifier: Apache-2.0
"""Focused contracts for the doctor shell-completions discovery hint (F12).

`tokenpak doctor` must point users at the safe, dry-run completions installer
without ever mutating shell startup files. These tests pin:
  * the read-only detection helper, and
  * the doctor row it renders (JSON + human), proven non-perturbing to the
    exit code by being recorded as a "pass" either way.
"""

from __future__ import annotations

import json

from tokenpak.cli.commands import doctor

DRY_RUN_HINT = "bash scripts/install-completions.sh --dry-run"


def _doctor_json(monkeypatch, tmp_path, capsys) -> dict:
    """Run run_doctor(--json) against an isolated HOME and return the payload."""
    fake_home = tmp_path / "home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    doctor.run_doctor(output_json=True)
    captured = capsys.readouterr().out
    last_brace = captured.rfind("\n{")
    json_text = captured[last_brace + 1 :] if last_brace != -1 else captured
    return json.loads(json_text)


def _completion_check(payload: dict) -> dict:
    matches = [c for c in payload["checks"] if c["check"] == "shell_completions"]
    assert len(matches) == 1, f"expected exactly one shell_completions check, got {matches}"
    return matches[0]


# --- detection helper -------------------------------------------------------


def test_shell_completions_present_finds_installed_file(tmp_path, monkeypatch):
    installed = tmp_path / ".bash_completion.d" / "tokenpak"
    installed.parent.mkdir(parents=True)
    installed.write_text("# completion\n")
    missing = tmp_path / "nope" / "tokenpak"

    monkeypatch.setattr(
        doctor, "_shell_completion_candidates", lambda: [missing, installed]
    )
    assert doctor._shell_completions_present() == installed


def test_shell_completions_absent_returns_none(tmp_path, monkeypatch):
    missing = tmp_path / "nope" / "_tokenpak"
    monkeypatch.setattr(doctor, "_shell_completion_candidates", lambda: [missing])
    assert doctor._shell_completions_present() is None


def test_shell_completion_candidates_cover_bash_zsh_fish(monkeypatch):
    monkeypatch.setenv("HOME", "/home/example")
    names = {p.name for p in doctor._shell_completion_candidates()}
    # One filename per supported shell, matching install-completions.sh.
    assert {"tokenpak", "_tokenpak", "tokenpak.fish"} <= names


# --- doctor rendering: completions absent -----------------------------------


def test_doctor_json_surfaces_dry_run_hint_when_absent(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "_shell_completions_present", lambda: None)
    payload = _doctor_json(monkeypatch, tmp_path, capsys)

    check = _completion_check(payload)
    assert check["status"] == "pass"
    assert DRY_RUN_HINT in check["message"]
    assert DRY_RUN_HINT in check["detail"]


def test_doctor_human_output_includes_dry_run_hint_when_absent(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(doctor, "_shell_completions_present", lambda: None)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    doctor.run_doctor(output_json=False)
    out = capsys.readouterr().out
    assert DRY_RUN_HINT in out


# --- doctor rendering: completions present ----------------------------------


def test_doctor_json_confirms_installed_path_when_present(tmp_path, monkeypatch, capsys):
    installed = tmp_path / ".config" / "fish" / "completions" / "tokenpak.fish"
    installed.parent.mkdir(parents=True)
    installed.write_text("# completion\n")
    monkeypatch.setattr(doctor, "_shell_completions_present", lambda: installed)

    payload = _doctor_json(monkeypatch, tmp_path, capsys)
    check = _completion_check(payload)
    assert check["status"] == "pass"
    assert "installed:" in check["message"]
    assert str(installed) in check["message"]
    # When already installed we confirm, we don't nag with the installer command.
    assert DRY_RUN_HINT not in check["message"]


# --- non-perturbing: the hint never changes exit semantics ------------------


def test_completion_check_status_is_pass_in_both_states(tmp_path, monkeypatch, capsys):
    for resolver in (lambda: None, lambda: tmp_path / ".bash_completion.d" / "tokenpak"):
        monkeypatch.setattr(doctor, "_shell_completions_present", resolver)
        payload = _doctor_json(monkeypatch, tmp_path, capsys)
        assert _completion_check(payload)["status"] == "pass"
