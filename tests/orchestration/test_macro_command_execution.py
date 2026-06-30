"""Injection-safety coverage for macro command execution (CP-02).

Macro steps and premade macros historically ran through the host shell. These
tests pin the governed execution model:

* a structured ``argv`` macro step runs with ``shell=False`` and passes payloads
  literally (no shell interpretation);
* the only remaining ``shell=True`` macro path is the legacy ``cmd`` string form,
  which is explicitly opt-in (``MacroStep.uses_shell``) and is *not* the form used
  for argv steps;
* dry-run executes nothing;
* premade macros (fixed ``tokenpak ...`` invocations) run as argv with
  ``shell=False``.
"""

from __future__ import annotations

import sys

import pytest

from tokenpak.orchestration.macros.engine import MacroDefinition, MacroEngine, MacroStep

_ECHO_ARG = [sys.executable, "-c", "import sys; print(sys.argv[1])"]


def _payload(sentinel) -> str:
    return f"; touch {sentinel} ; echo owned & whoami | cat"


# ── macro engine: argv steps are shell-free ───────────────────────────────────


def test_argv_step_passes_payload_literally(tmp_path):
    sentinel = tmp_path / "PWNED"
    payload = _payload(sentinel)
    step = MacroStep(name="echo", argv=[*_ECHO_ARG, payload])
    assert step.uses_shell is False

    engine = MacroEngine(macros_dir=tmp_path)
    result = engine.run_definition(MacroDefinition(name="m", steps=[step]))

    assert result.steps[0].success is True
    assert result.steps[0].output.strip() == payload
    assert not sentinel.exists()  # ';touch' never executed -> no shell


def test_argv_step_does_not_need_trusted_optin(tmp_path):
    # argv steps carry no shell-string risk, so running them does not emit the
    # trusted-user-code notice that shell ``cmd`` steps do.
    step = MacroStep(name="ver", argv=[sys.executable, "--version"])
    engine = MacroEngine(macros_dir=tmp_path)
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = engine.run_definition(MacroDefinition(name="m", steps=[step]))
    assert result.steps[0].returncode == 0
    assert not any("shell=True" in str(w.message) for w in caught)


# ── macro engine: legacy cmd path is opt-in shell ─────────────────────────────


def test_cmd_string_step_is_marked_shell():
    # The remaining shell=True macro form is the legacy ``cmd`` string; it is
    # explicitly distinguishable from the default argv form.
    shell_step = MacroStep(name="s", cmd="echo hi")
    argv_step = MacroStep(name="a", argv=["echo", "hi"])
    assert shell_step.uses_shell is True
    assert argv_step.uses_shell is False


def test_cmd_string_step_warns_without_trusted_optin(tmp_path):
    import warnings

    step = MacroStep(name="s", cmd="echo hi")
    engine = MacroEngine(macros_dir=tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        engine.run_definition(MacroDefinition(name="m", steps=[step]), trusted_user_code=None)
    assert any("trusted user code" in str(w.message).lower() for w in caught)


# ── macro engine: dry-run ─────────────────────────────────────────────────────


def test_dry_run_executes_no_step(tmp_path):
    sentinel = tmp_path / "created"
    step = MacroStep(
        name="w", argv=[sys.executable, "-c", f"open({str(sentinel)!r},'w').close()"]
    )
    engine = MacroEngine(macros_dir=tmp_path)
    result = engine.run_definition(MacroDefinition(name="m", steps=[step]), dry_run=True)
    assert result.dry_run is True
    assert not sentinel.exists()


# ── premade macros: argv, shell=False ─────────────────────────────────────────


def test_premade_step_runs_as_argv(monkeypatch):
    from tokenpak.orchestration import commands as commands_mod
    from tokenpak.orchestration.macros.premade_macros import PremadeMacroRunner

    seen = {}
    real = commands_mod.run_command_action

    def spy(action, **kwargs):
        seen["use_shell"] = action.use_shell
        seen["argv"] = action.argv
        return real(action, **kwargs)

    monkeypatch.setattr(commands_mod, "run_command_action", spy)
    PremadeMacroRunner()._run_step(
        {"name": "n", "label": "l", "cmd": "tokenpak status --json"}
    )
    assert seen["use_shell"] is False
    assert seen["argv"] == ("tokenpak", "status", "--json")


def test_premade_step_payload_is_literal(tmp_path):
    from tokenpak.orchestration.macros.premade_macros import PremadeMacroRunner

    sentinel = tmp_path / "PWNED"
    payload = _payload(sentinel)
    # A crafted step proves the runner does not shell-interpret its command.
    import shlex

    cmd = " ".join(shlex.quote(p) for p in [*_ECHO_ARG, payload])
    out = PremadeMacroRunner()._run_step({"name": "n", "label": "l", "cmd": cmd})
    assert out["returncode"] == 0
    assert payload in out["output"]
    assert not sentinel.exists()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
