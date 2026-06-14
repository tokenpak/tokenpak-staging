# SPDX-License-Identifier: Apache-2.0
"""Offline contract tests for ``tokenpak cards`` CLI (Std 54 §L).

Tests use the in-process argparse builder + handler functions rather
than ``subprocess`` — same convention as ``test_pak_command.py``. The
smoke test at the bottom exercises the real entry point once.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tokenpak.cli.commands.cards import (
    build_cards_parser,
    cmd_cards_compile,
    cmd_cards_discover,
    cmd_cards_doctor,
    cmd_cards_inspect,
    cmd_cards_install,
    cmd_cards_list,
    cmd_cards_preview,
    cmd_cards_scaffold,
    cmd_cards_validate,
)
from tokenpak.cli.commands.pak import build_pak_parser

ALL_ACTIONS = (
    "discover",
    "validate",
    "compile",
    "install",
    "list",
    "inspect",
    "preview",
    "scaffold",
    "doctor",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_parser():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    build_cards_parser(sub)
    return parser


def _args(**kw):
    base = {
        "card_type": None,
        "mode": "dev",
        "strict": False,
        "json": False,
        "path": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A scaffolded project as CWD with one tip + one pak card."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".tokenpak.md").write_text("# Project index\n", encoding="utf-8")
    rc = cmd_cards_scaffold(
        SimpleNamespace(card_type="tip", kind="provider_adapter", name="acme")
    )
    assert rc == 0
    rc = cmd_cards_scaffold(
        SimpleNamespace(card_type="pak", kind="provider_adapter", name="notes")
    )
    assert rc == 0
    return tmp_path


# ---------------------------------------------------------------------------
# Argparse registration (Std 54 §L surface)
# ---------------------------------------------------------------------------


def test_parser_registers_cards_subcommand():
    parser = _make_parser()
    args = parser.parse_args(["cards", "list"])
    assert args.command == "cards"
    assert args.cards_action == "list"


def test_parser_registers_all_phase1_actions():
    parser = _make_parser()
    for action in ALL_ACTIONS:
        argv = ["cards", action]
        if action in ("inspect", "preview"):
            argv.append("some-card")
        elif action == "scaffold":
            argv += ["--type", "pak", "--name", "x"]
        args = parser.parse_args(argv)
        assert args.cards_action == action


def test_parser_accepts_spec_flags():
    parser = _make_parser()
    args = parser.parse_args(
        ["cards", "validate", "--type", "tip", "--mode", "locked", "--strict"]
    )
    assert args.card_type == "tip"
    assert args.mode == "locked"
    assert args.strict is True
    args = parser.parse_args(["cards", "preview", "x", "--pro", "--query", "q"])
    assert args.pro is True
    assert args.query == "q"


def test_worker_type_accepted_by_parser_but_rejected_by_handler(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parser = _make_parser()
    args = parser.parse_args(["cards", "discover", "--type", "worker"])
    assert args.card_type == "worker"  # §L surface accepts it
    rc = cmd_cards_discover(args)
    assert rc == 1  # Phase 2 — clear user error, not a crash


def test_cards_and_pak_are_distinct_verbs():
    """Invariant 13: `cards` and `pak` are not aliases — different verbs,
    different action namespaces, different artifacts."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    build_pak_parser(sub)
    build_cards_parser(sub)  # registering both must not collide

    cards_args = parser.parse_args(["cards", "list"])
    pak_args = parser.parse_args(["pak", "status"])
    assert cards_args.command == "cards"
    assert pak_args.command == "pak"
    assert hasattr(cards_args, "cards_action")
    assert not hasattr(cards_args, "pak_action")
    assert hasattr(pak_args, "pak_action")
    assert not hasattr(pak_args, "cards_action")
    # `cards inspect` takes an authoring-source name; `pak inspect` takes
    # a runtime Pak ref — both parse independently.
    assert parser.parse_args(["cards", "inspect", "n"]).func is cmd_cards_inspect
    assert parser.parse_args(["pak", "inspect", "pak:x"]).func is not cmd_cards_inspect


# ---------------------------------------------------------------------------
# Handler pipeline (scaffold → discover → validate → compile → install → list)
# ---------------------------------------------------------------------------


def test_discover_finds_scaffolded_cards(project, capsys):
    rc = cmd_cards_discover(_args(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    kinds = sorted(c["card_kind"] for c in payload["cards"])
    assert kinds == ["pak", "tip"]
    assert payload["project_index_present"] is True


def test_discover_refused_in_locked_mode(project):
    rc = cmd_cards_discover(_args(mode="locked"))
    assert rc == 1  # §G: locked = no project-tree discovery


def test_validate_all_passes_on_scaffolded_project(project, capsys):
    rc = cmd_cards_validate(_args(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert len(payload["cards"]) == 2


def test_validate_reports_errors_with_exit_1(project, capsys):
    bad = project / "paks" / "bad.pak.md"
    bad.write_text("---\ncard_kind: pak\n---\nbody\n", encoding="utf-8")
    rc = cmd_cards_validate(_args(json=True))
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False


def test_compile_writes_manifests_into_project_cache(project, capsys):
    rc = cmd_cards_compile(_args())
    assert rc == 0
    compiled = project / ".tokenpak" / "cache" / "cards" / "compiled"
    names = sorted(p.name for p in compiled.glob("*.json"))
    assert names == ["acme.json", "notes.json"]
    manifest = json.loads((compiled / "notes.json").read_text(encoding="utf-8"))
    assert manifest["target_contract"] == "tokenpak.tip.Pak"
    assert manifest["pak"]["privacy"] == {"class": "local_only"}


def test_install_records_in_installed_manifest(project, capsys):
    rc = cmd_cards_install(_args())
    assert rc == 0
    store = json.loads(
        (project / ".tokenpak" / "cache" / "cards" / "installed.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(store["cards"]) == {"acme", "notes"}
    assert store["cards"]["notes"]["card_kind"] == "pak"


def test_list_locked_mode_shows_installed_only(project, capsys):
    cmd_cards_install(_args())
    capsys.readouterr()
    rc = cmd_cards_list(_args(mode="locked", json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["discovered"] == []  # §G: no project-tree discovery
    assert set(payload["installed"]) == {"acme", "notes"}


def test_inspect_shows_static_declarations(project, capsys):
    rc = cmd_cards_inspect(_args(name="notes", json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["card_kind"] == "pak"
    assert payload["target_contract"] == "tokenpak.tip.Pak"
    assert payload["valid"] is True
    assert payload["pak_fields"]["privacy"] == "local_only"


def test_inspect_unknown_name_errors(project):
    rc = cmd_cards_inspect(_args(name="missing-card"))
    assert rc == 1


def test_preview_is_static_and_unranked(project, capsys):
    rc = cmd_cards_preview(_args(name="notes", pro=False, query=None))
    assert rc == 0
    out = capsys.readouterr().out
    assert "unranked" in out
    assert "Pro Local" in out


def test_preview_pro_falls_back_when_daemon_absent(project, capsys, monkeypatch):
    import tokenpak.licensing.daemon_probe as probe

    monkeypatch.setattr(probe, "detect_daemon_state", lambda **kw: "unavailable")
    rc = cmd_cards_preview(_args(name="notes", pro=True, query="q", json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pro_daemon_state"] == "unavailable"
    assert payload["live_candidates"] is None


def test_doctor_passes_on_clean_project(project, capsys):
    cmd_cards_install(_args())
    capsys.readouterr()
    rc = cmd_cards_doctor(_args(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True


def test_doctor_fails_on_invalid_card(project, capsys):
    (project / "paks" / "bad.pak.md").write_text(
        "---\ncard_kind: pak\n---\nbody\n", encoding="utf-8"
    )
    rc = cmd_cards_doctor(_args(json=True))
    assert rc == 1


def test_strict_validate_requires_adapter_equality(project, capsys):
    """--strict flows through to the §H equality check."""
    adapter = project / "integrations" / "acme" / "adapter.py"
    adapter.write_text(
        "capabilities = frozenset({'tip.compression.v1'})\n", encoding="utf-8"
    )
    # Card declares [] but adapter declares one capability → strict fails.
    rc = cmd_cards_validate(_args(strict=True, json=True))
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    flat_errors = [e for c in payload["cards"] for e in c["errors"]]
    assert any("strict mode requires" in e for e in flat_errors)


# ---------------------------------------------------------------------------
# Entry-point smoke
# ---------------------------------------------------------------------------


def test_cards_help_exits_zero():
    result = subprocess.run(
        [sys.executable, "-m", "tokenpak", "cards", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "authoring" in result.stdout
