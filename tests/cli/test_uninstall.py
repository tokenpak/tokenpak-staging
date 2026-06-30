# SPDX-License-Identifier: Apache-2.0
"""Behavioral coverage for ``tokenpak uninstall`` (run_uninstall).

The most destructive verb in the CLI previously shipped with zero behavioral
tests. These exercise the real plan-build / purge / dry-run / json / safety-gate
logic of ``tokenpak.cli.commands.uninstall.run_uninstall`` and assert the
safe-by-default invariants the command documents:

  * ``--soft`` un-routes only — it deletes no user state.
  * ``--hard`` purges resolved-home state BUT retains the ``_COMPANION_PROTECTED``
    carve-out (journal.db / budget.db / capsules/).
  * ``--dry-run`` is structurally side-effect-free (op-plan only).
  * non-interactive ``--hard`` REFUSES without ``--yes`` (no destructive default).
  * ``--json`` emits a stable receipt shape.

Hard sandbox discipline (packet stop_conditions)
------------------------------------------------
Every test runs inside the autouse ``sandbox`` fixture, which is the ONLY thing
any destructive path is ever allowed to touch:

  1. ``TOKENPAK_HOME`` is pointed at a tmp dir → ``_paths.home()`` (the purge
     root) resolves into the sandbox, never the live install.
  2. ``Path.home()`` is patched to a tmp dir → Claude Code settings, the codex
     companion, and the legacy ``~/.tokenpak`` proxy pidfile all resolve into
     the sandbox.
  3. ``subprocess.run`` is stubbed → ``pip uninstall tokenpak`` can NEVER fire
     for real, even on the ``--hard --yes`` path. This is the load-bearing guard
     against mutating the test interpreter's environment.
  4. ``builtins.input`` raises by default → an unexpected prompt fails loudly
     instead of blocking on / reading real stdin. (This also doubles as the
     proof that ``--yes`` never prompts.)

The codex teardown is stubbed to a deterministic no-op: it has its own coverage,
and these tests assert the uninstall *orchestrator*, not codex internals.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tokenpak.cli.commands import uninstall as uninstall_mod

# ---------------------------------------------------------------------------
# Sandbox helpers
# ---------------------------------------------------------------------------


def _no_prompt(prompt: str = "") -> str:
    raise AssertionError(f"unexpected interactive prompt: {prompt!r}")


def _fake_pip_run(cmd, *args, **kwargs):
    """Stand-in for subprocess.run — proves the wiring without ever invoking pip.

    Asserts the orchestrator only ever asks to remove the tokenpak package, then
    reports a clean uninstall so the package phase records ``done``.
    """
    assert "uninstall" in cmd and cmd[-1] == "tokenpak", f"unexpected subprocess: {cmd}"
    return subprocess.CompletedProcess(cmd, 0, stdout="Successfully uninstalled tokenpak", stderr="")


def _fake_tty(monkeypatch) -> None:
    """Make the current stdin/stdout report as a TTY (for interactive paths)."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)


def _populate_home(home: Path) -> Path:
    """Lay down a realistic resolved-home tree. Returns the companion/ path.

    Mixes purge targets (config/dbs/cache/templates + non-protected companion
    entries) with the protected user-data carve-out, so a --hard run has to make
    the keep/delete decision for real.
    """
    (home / "config.json").write_text('{"k": 1}')
    (home / "config.yaml").write_text("k: 1\n")
    (home / "telemetry.db").write_bytes(b"\x00telemetry")
    (home / "monitor.db").write_bytes(b"\x00monitor")
    (home / "requests.jsonl").write_text("{}\n")
    cache = home / "cache"
    cache.mkdir()
    (cache / "blob").write_text("x")
    templates = home / "templates"
    templates.mkdir()
    (templates / "t.md").write_text("x")

    companion = home / "companion"
    companion.mkdir()
    # Protected user data — must survive --hard.
    (companion / "journal.db").write_bytes(b"JOURNAL")
    (companion / "budget.db").write_bytes(b"BUDGET")
    capsules = companion / "capsules"
    capsules.mkdir()
    (capsules / "cap1.bin").write_bytes(b"CAPSULE")
    # Non-protected companion entries — must be purged under --hard.
    (companion / "mcp_state.json").write_text("{}")
    codexdir = companion / "codex"
    codexdir.mkdir()
    (codexdir / "hooks.json").write_text("{}")
    return companion


def _tree_snapshot(root: Path) -> dict:
    """Map of relative path -> bytes (files) / None (dirs) for the whole tree."""
    snap: dict = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        snap[rel + "/" if p.is_dir() else rel] = None if p.is_dir() else p.read_bytes()
    return snap


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Isolate every destructive path into a tmp tree (see module docstring)."""
    fake_home = tmp_path / "home"
    tpk_home = fake_home / ".tpk"
    tpk_home.mkdir(parents=True)

    # 1. Purge root → sandbox. _paths.home() honors TOKENPAK_HOME first (the
    #    module docstring calls this the sandbox override).
    monkeypatch.setenv("TOKENPAK_HOME", str(tpk_home))
    # 2. Path.home() → sandbox (Claude settings / codex / legacy ~/.tokenpak pid).
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    # 3. Deterministic, color-free receipts.
    monkeypatch.setenv("NO_COLOR", "1")
    # 4. HARD GUARD: never let `pip uninstall tokenpak` run for real.
    monkeypatch.setattr(uninstall_mod.subprocess, "run", _fake_pip_run)
    # 5. Keep the orchestrator hermetic from codex internals.
    monkeypatch.setattr(
        uninstall_mod,
        "_teardown_codex",
        lambda: (uninstall_mod._OUTCOME_NOOP, "codex stubbed in test"),
    )
    # 6. Any unexpected prompt fails loudly (also: --yes must never prompt).
    monkeypatch.setattr("builtins.input", _no_prompt)

    return SimpleNamespace(fake_home=fake_home, home=tpk_home)


# ---------------------------------------------------------------------------
# Carve-out definition
# ---------------------------------------------------------------------------


def test_companion_protected_set_is_journal_budget_capsules():
    # Lock the carve-out definition so a silent change is caught here.
    assert uninstall_mod._COMPANION_PROTECTED == ("journal.db", "budget.db", "capsules")


# ---------------------------------------------------------------------------
# --soft (un-route only)
# ---------------------------------------------------------------------------


def test_soft_is_unroute_only_no_data_deleted(sandbox, capsys):
    _populate_home(sandbox.home)
    before = _tree_snapshot(sandbox.home)

    rc = uninstall_mod.run_uninstall(soft=True)

    out = capsys.readouterr().out
    assert rc == 0
    assert _tree_snapshot(sandbox.home) == before  # soft deletes nothing
    assert "tokenpak setup" in out  # reversible-by-default hint


def test_dry_run_soft_is_side_effect_free(sandbox):
    _populate_home(sandbox.home)
    before = _tree_snapshot(sandbox.home)

    rc = uninstall_mod.run_uninstall(soft=True, dry_run=True)

    assert rc == 0
    assert _tree_snapshot(sandbox.home) == before


# ---------------------------------------------------------------------------
# --hard safety gate (no destructive default; refuse without --yes)
# ---------------------------------------------------------------------------


def test_hard_noninteractive_refuses_without_yes(sandbox, capsys):
    _populate_home(sandbox.home)
    before = _tree_snapshot(sandbox.home)

    rc = uninstall_mod.run_uninstall(hard=True)  # non-TTY, no --yes

    err = capsys.readouterr().err
    assert rc == 2
    assert "--hard requires --yes" in err
    assert _tree_snapshot(sandbox.home) == before  # refusal touches nothing


def test_no_destructive_default_refuses(sandbox, capsys):
    _populate_home(sandbox.home)
    before = _tree_snapshot(sandbox.home)

    rc = uninstall_mod.run_uninstall()  # no --soft / --hard, non-TTY

    err = capsys.readouterr().err
    assert rc == 2
    assert "specify --soft" in err
    assert _tree_snapshot(sandbox.home) == before


def test_soft_and_hard_conflict_refused(sandbox, capsys):
    rc = uninstall_mod.run_uninstall(soft=True, hard=True)

    err = capsys.readouterr().err
    assert rc == 2
    assert "only one of --soft / --hard" in err


def test_hard_json_error_receipt_on_refusal(sandbox, capsys):
    _populate_home(sandbox.home)

    rc = uninstall_mod.run_uninstall(hard=True, output_json=True)  # non-TTY, no --yes

    err = capsys.readouterr().err
    assert rc == 2
    payload = json.loads(err)
    assert "error" in payload
    assert "--yes" in payload["error"]


# ---------------------------------------------------------------------------
# --hard purge + protected-data retention
# ---------------------------------------------------------------------------


def test_hard_yes_purges_state_and_retains_companion_protected(sandbox):
    companion = _populate_home(sandbox.home)

    rc = uninstall_mod.run_uninstall(hard=True, yes=True)

    assert rc == 0
    # State is purged.
    for name in (
        "config.json",
        "config.yaml",
        "telemetry.db",
        "monitor.db",
        "requests.jsonl",
        "cache",
        "templates",
    ):
        assert not (sandbox.home / name).exists(), f"{name} should be purged"
    # Non-protected companion entries are purged.
    assert not (companion / "mcp_state.json").exists()
    assert not (companion / "codex").exists()
    # Protected user data survives, contents intact.
    assert (companion / "journal.db").read_bytes() == b"JOURNAL"
    assert (companion / "budget.db").read_bytes() == b"BUDGET"
    assert (companion / "capsules").is_dir()
    assert (companion / "capsules" / "cap1.bin").read_bytes() == b"CAPSULE"


def test_yes_bypasses_all_prompts(sandbox):
    # builtins.input is patched to raise; a clean run proves --yes never prompts.
    companion = _populate_home(sandbox.home)

    rc = uninstall_mod.run_uninstall(hard=True, yes=True)

    assert rc == 0
    assert not (sandbox.home / "config.json").exists()
    assert (companion / "journal.db").exists()


def test_keep_data_retains_entire_home(sandbox):
    _populate_home(sandbox.home)
    before = _tree_snapshot(sandbox.home)

    rc = uninstall_mod.run_uninstall(hard=True, yes=True, keep_data=True)

    assert rc == 0
    assert _tree_snapshot(sandbox.home) == before  # --keep-data deletes nothing


# ---------------------------------------------------------------------------
# --dry-run (side-effect-free)
# ---------------------------------------------------------------------------


def test_dry_run_hard_is_side_effect_free(sandbox, capsys):
    _populate_home(sandbox.home)
    before = _tree_snapshot(sandbox.home)

    # No --yes required: dry-run bypasses the destructive gate and removes nothing.
    rc = uninstall_mod.run_uninstall(hard=True, dry_run=True)

    out = capsys.readouterr().out
    assert rc == 0
    assert _tree_snapshot(sandbox.home) == before
    assert "dry-run" in out
    assert "WOULD" in out


# ---------------------------------------------------------------------------
# --json receipt shape
# ---------------------------------------------------------------------------


def test_json_receipt_shape_hard_dry_run(sandbox, capsys):
    companion = _populate_home(sandbox.home)

    rc = uninstall_mod.run_uninstall(hard=True, dry_run=True, output_json=True)

    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert set(payload) >= {
        "mode",
        "dry_run",
        "keep_data",
        "home",
        "operations",
        "retained",
        "errors",
    }
    assert payload["mode"] == "hard"
    assert payload["dry_run"] is True
    assert payload["keep_data"] is False
    assert payload["home"] == str(sandbox.home)
    assert payload["errors"] == 0
    assert isinstance(payload["operations"], list) and payload["operations"]
    for op in payload["operations"]:
        assert set(op) >= {"phase", "op", "outcome", "detail"}
    # Dry-run: every operation is a no-op skip (the side-effect-free contract).
    assert {op["outcome"] for op in payload["operations"]} == {"skip"}
    # Retained list surfaces the protected user data.
    retained = set(payload["retained"])
    assert str(companion / "journal.db") in retained
    assert str(companion / "budget.db") in retained
    assert str(companion / "capsules") in retained


def test_json_receipt_shape_soft(sandbox, capsys):
    _populate_home(sandbox.home)

    rc = uninstall_mod.run_uninstall(soft=True, output_json=True)

    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["mode"] == "soft"
    assert payload["dry_run"] is False
    assert payload["keep_data"] is False
    assert payload["retained"] == []
    assert payload["errors"] == 0
    # Soft plan is three soft-phase ops; no hard/package phase.
    assert {op["phase"] for op in payload["operations"]} == {"soft"}


def test_pip_uninstall_recorded_under_hard_yes(sandbox, capsys):
    _populate_home(sandbox.home)

    rc = uninstall_mod.run_uninstall(hard=True, yes=True, output_json=True)

    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    pkg = [op for op in payload["operations"] if op["phase"] == "package"]
    assert len(pkg) == 1
    assert pkg[0]["outcome"] == "done"  # stubbed pip reports a clean removal
    assert "tokenpak" in pkg[0]["op"]


# ---------------------------------------------------------------------------
# Interactive confirmation paths (TTY)
# ---------------------------------------------------------------------------


def test_hard_interactive_abort_deletes_nothing(sandbox, capsys, monkeypatch):
    _populate_home(sandbox.home)
    before = _tree_snapshot(sandbox.home)
    _fake_tty(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    rc = uninstall_mod.run_uninstall(hard=True)  # interactive, answered "n"

    capsys.readouterr()
    assert rc == 2
    assert _tree_snapshot(sandbox.home) == before  # abort touches nothing


def test_hard_interactive_confirm_purges_and_retains(sandbox, capsys, monkeypatch):
    companion = _populate_home(sandbox.home)
    _fake_tty(monkeypatch)
    # "y" answers both the destructive confirm AND the secondary pip prompt.
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    rc = uninstall_mod.run_uninstall(hard=True)  # interactive, answered "y"

    capsys.readouterr()
    assert rc == 0
    assert not (sandbox.home / "config.json").exists()
    assert (companion / "journal.db").exists()  # carve-out holds interactively too


# ---------------------------------------------------------------------------
# Proxy-stop primitive (safe: no real signal sent)
# ---------------------------------------------------------------------------


def test_soft_cleans_unreadable_proxy_pidfile(sandbox):
    # Unparseable pid is removed as cleanup — _stop_proxy never calls os.kill here.
    (sandbox.home / "proxy.pid").write_text("not-a-pid")

    rc = uninstall_mod.run_uninstall(soft=True)

    assert rc == 0
    assert not (sandbox.home / "proxy.pid").exists()
