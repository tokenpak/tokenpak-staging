"""Focused tests for the macro/skill shell-execution trusted-user-code contract.

Covers the security hardening contract added for shell-backed macro/skill
execution:

- Shell-backed macro runs surface a trusted-user-code notice by default, and
  the explicit opt-in (env var or ``trusted_user_code=`` argument) suppresses it
  without changing behavior.
- Generated skill macros preserve functionality and prefer structured argv
  (``shell=False``) while keeping the YAML command-string capability.
- Promotion of a repeated pattern into a skill never auto-fires execution
  (no ``trigger_pattern`` activity matcher).
- Macro/skill file loading warns on shared-home tamper indicators (group/world
  writable or symlinked files).

These tests target the live ``tokenpak.orchestration.macros`` /
``tokenpak.orchestration.skill_compiler`` paths.
"""

from __future__ import annotations

import os
import warnings
from unittest.mock import patch

import pytest

pytest.importorskip("yaml", reason="PyYAML required for the macro engine")

from tokenpak.orchestration.macros.engine import (
    _TRUSTED_USER_CODE_ENV,
    MacroEngine,
    MacroStep,
    _check_file_integrity,
    _MacroIntegrityWarning,
    _MacroTrustedCodeWarning,
    _trusted_user_code_enabled,
)
from tokenpak.orchestration.macros.scheduler import MacroScheduler
from tokenpak.orchestration.skill_compiler import SkillCompiler, SkillEpisode, SkillStore

# Fixtures


@pytest.fixture
def no_optin(monkeypatch):
    """Ensure the trusted-user-code opt-in is unset for the test."""
    monkeypatch.delenv(_TRUSTED_USER_CODE_ENV, raising=False)


@pytest.fixture
def engine(tmp_path):
    return MacroEngine(macros_dir=tmp_path / "macros")


def _make_echo_macro(engine: MacroEngine, name: str = "echo-macro") -> None:
    engine.create(name, [{"name": "hi", "cmd": "echo trusted-ok", "label": "Echo"}])


# Opt-in / warning behavior


def test_shell_macro_warns_by_default(engine, no_optin):
    _make_echo_macro(engine)
    with pytest.warns(_MacroTrustedCodeWarning):
        result = engine.run("echo-macro")
    # Functionality preserved: the macro still executed.
    assert result.success is True
    assert "trusted-ok" in result.steps[0].output


def test_env_optin_suppresses_warning(engine, monkeypatch):
    monkeypatch.setenv(_TRUSTED_USER_CODE_ENV, "1")
    _make_echo_macro(engine)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = engine.run("echo-macro")
    assert not [w for w in caught if issubclass(w.category, _MacroTrustedCodeWarning)]
    assert result.success is True


def test_argument_optin_suppresses_warning(engine, no_optin):
    _make_echo_macro(engine)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = engine.run("echo-macro", trusted_user_code=True)
    assert not [w for w in caught if issubclass(w.category, _MacroTrustedCodeWarning)]
    assert result.success is True


def test_dry_run_does_not_warn(engine, no_optin):
    _make_echo_macro(engine)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = engine.run("echo-macro", dry_run=True)
    assert not [w for w in caught if issubclass(w.category, _MacroTrustedCodeWarning)]
    assert result.dry_run is True


def test_optin_resolution_precedence(monkeypatch):
    monkeypatch.delenv(_TRUSTED_USER_CODE_ENV, raising=False)
    assert _trusted_user_code_enabled() is False
    assert _trusted_user_code_enabled(True) is True
    assert _trusted_user_code_enabled(False) is False
    monkeypatch.setenv(_TRUSTED_USER_CODE_ENV, "yes")
    assert _trusted_user_code_enabled() is True
    # Explicit argument still overrides the environment.
    assert _trusted_user_code_enabled(False) is False


# Structured argv vs shell command string


def test_argv_step_runs_without_shell(engine, no_optin):
    """argv steps run shell=False; shell metacharacters are passed literally."""
    engine.create(
        "argv-macro",
        [{"name": "echo", "label": "Echo", "argv": ["echo", "a; echo b"]}],
    )
    step = engine.show("argv-macro").steps[0]
    assert step.uses_shell is False
    result = engine.run("argv-macro")
    assert result.success is True
    # With a shell, "a; echo b" would run two commands; shell=False keeps it literal.
    assert result.steps[0].output == "a; echo b"


def test_argv_only_macro_does_not_warn(engine, no_optin):
    """A macro with no shell command strings raises no trusted-code notice."""
    engine.create("argv-macro", [{"name": "echo", "argv": ["echo", "hi"]}])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        engine.run("argv-macro")
    assert not [w for w in caught if issubclass(w.category, _MacroTrustedCodeWarning)]


def test_cmd_string_capability_preserved(engine, no_optin):
    """cmd strings still execute via the shell (shell features work)."""
    engine.create(
        "shell-macro",
        [{"name": "both", "label": "Both", "cmd": "echo hi && echo bye"}],
    )
    step = engine.show("shell-macro").steps[0]
    assert step.uses_shell is True
    with pytest.warns(_MacroTrustedCodeWarning):
        result = engine.run("shell-macro")
    assert "hi" in result.steps[0].output and "bye" in result.steps[0].output


def test_macrostep_argv_roundtrips_and_cmd_only_unchanged():
    """argv serializes only when present; cmd-only steps round-trip unchanged."""
    cmd_only = MacroStep(name="s", cmd="echo hi").to_dict()
    assert "argv" not in cmd_only  # no schema change for existing cmd macros
    structured = MacroStep(name="s", cmd="t --k 7", argv=["t", "--k", 7]).to_dict()
    assert structured["argv"] == ["t", "--k", "7"]
    assert MacroStep.from_dict(structured).argv == ["t", "--k", "7"]


# Skill compiler: generated argv + no auto-fire


def test_generated_skill_steps_use_structured_argv(tmp_path):
    store = SkillStore(skills_dir=tmp_path / "skills", macro_engine=MacroEngine(tmp_path / "macros"))
    steps = store._build_macro_steps(
        [{"tool": "echo_tool", "args": {"message": "converted"}, "label": "Echo"}]
    )
    assert len(steps) == 1
    # cmd retained (display / YAML command-string parity)...
    assert "echo_tool" in steps[0]["cmd"] and "converted" in steps[0]["cmd"]
    # ...and a structured argv is produced for shell-free execution.
    assert steps[0]["argv"] == ["echo_tool", "--message", "converted"]


def _tool_episode(target: str, idx: int) -> SkillEpisode:
    return SkillEpisode(
        task_type="run_tool",
        tool_sequence=["mytool"],
        file_targets=[target],
        steps=[{"tool": "echo_tool", "args": {"message": "converted"}, "label": "Echo"}],
        validation="output contains converted",
        success=True,
        validation_passed=True,
        tokens_original=100,
        tokens_skill=50,
        timestamp=f"2026-03-11T16:0{idx}:00+00:00",
    )


def test_promotion_does_not_auto_execute(tmp_path):
    """Recording/promoting a skill must never auto-fire shell execution."""
    store = SkillStore(skills_dir=tmp_path / "skills", macro_engine=MacroEngine(tmp_path / "macros"))
    compiler = SkillCompiler(store=store)
    with patch("subprocess.run") as run_spy:
        skill = None
        for idx in range(3):
            skill = compiler.record_episode(_tool_episode(str(tmp_path / "f.txt"), idx))
        # Promotion happened (skill registered as a macro)...
        assert skill is not None
        assert store.macro_engine.exists(skill.skill_id)
        # ...but nothing was ever executed during record/promote.
        run_spy.assert_not_called()


def test_generated_skill_macro_runs_without_shell(tmp_path, no_optin):
    store = SkillStore(skills_dir=tmp_path / "skills", macro_engine=MacroEngine(tmp_path / "macros"))
    compiler = SkillCompiler(store=store)
    skill = None
    for idx in range(3):
        skill = compiler.record_episode(_tool_episode(str(tmp_path / "f.txt"), idx))
    macro = store.macro_engine.show(skill.skill_id)
    # Generated skill macro preserves functionality but is structured (shell-free).
    assert macro.steps[0].uses_shell is False
    assert macro.steps[0].argv and macro.steps[0].argv[0] == "echo_tool"


# Integrity guard


def test_world_writable_macro_warns(engine, no_optin):
    _make_echo_macro(engine, "perm-macro")
    path = engine._path("perm-macro")
    os.chmod(path, 0o666)  # group + world writable: tamper indicator
    with pytest.warns(_MacroIntegrityWarning):
        engine.show("perm-macro")


def test_clean_permissions_no_integrity_warning(engine, no_optin):
    _make_echo_macro(engine, "clean-macro")
    path = engine._path("clean-macro")
    os.chmod(path, 0o600)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        engine.show("clean-macro")
    assert not [w for w in caught if issubclass(w.category, _MacroIntegrityWarning)]


def test_check_file_integrity_flags_symlink(tmp_path):
    real = tmp_path / "real.yaml"
    real.write_text("name: x\nsteps: []\n")
    os.chmod(real, 0o600)
    link = tmp_path / "link.yaml"
    link.symlink_to(real)
    indicators = _check_file_integrity(link)
    assert any("symlink" in note for note in indicators)


def test_check_file_integrity_absent_is_clean(tmp_path):
    assert _check_file_integrity(tmp_path / "nope.yaml") == []


# Scheduler surfaces the contract before scheduling


@pytest.fixture
def safe_scheduler(tmp_path, monkeypatch):
    """A scheduler that never touches the real crontab/at on the host."""
    sched = MacroScheduler(schedule_path=tmp_path / "scheduled.json")
    monkeypatch.setattr(sched, "_add_cron_entry", lambda *a, **k: True)
    monkeypatch.setattr(sched, "_schedule_at_command", lambda *a, **k: True)
    return sched


def test_schedule_cron_warns_before_scheduling(safe_scheduler, no_optin):
    with pytest.warns(_MacroTrustedCodeWarning):
        rec = safe_scheduler.schedule_cron("echo-macro", "0 9 * * *")
    assert rec.name == "echo-macro"
    assert rec.schedule_type == "cron"


def test_schedule_at_warns_before_scheduling(safe_scheduler, no_optin):
    with pytest.warns(_MacroTrustedCodeWarning):
        rec = safe_scheduler.schedule_at("echo-macro", "now + 1 hour")
    assert rec.schedule_type == "at"


def test_schedule_optin_suppresses_warning(safe_scheduler, monkeypatch):
    monkeypatch.setenv(_TRUSTED_USER_CODE_ENV, "1")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        safe_scheduler.schedule_cron("echo-macro", "0 9 * * *")
    assert not [w for w in caught if issubclass(w.category, _MacroTrustedCodeWarning)]
