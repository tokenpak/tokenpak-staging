# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import io
import json
import os
import stat
import sys

import pytest

from tokenpak import _cli_core


def _parse_activate(args: list[str]):
    return _cli_core.build_parser().parse_args(["activate", *args])


def _activate_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("TOKENPAK_HOME", str(home))
    monkeypatch.delenv("TOKENPAK_LICENSE_FILE", raising=False)
    monkeypatch.setattr(
        "tokenpak.licensing.daemon_probe.detect_daemon_state",
        lambda: "unavailable",
    )
    return home


def _license_data(home):
    return json.loads((home / "license.json").read_text(encoding="utf-8"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode checks are not portable")
def test_activate_writes_owner_only_license_store(tmp_path, monkeypatch):
    home = _activate_env(tmp_path, monkeypatch)
    args = _parse_activate(["OWNER-ONLY-LICENSE-KEY-0001"])

    assert args.func(args) == 0

    license_path = home / "license.json"
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE(license_path.stat().st_mode) == 0o600


def test_activate_key_file_route_stores_key(tmp_path, monkeypatch):
    home = _activate_env(tmp_path, monkeypatch)
    key_file = tmp_path / "license-key.txt"
    key_file.write_text("FILE-ROUTE-LICENSE-KEY-0001\n", encoding="utf-8")
    args = _parse_activate(["--key-file", str(key_file), "--email", "ops@example.invalid"])

    assert args.func(args) == 0

    data = _license_data(home)
    assert data["key"] == "FILE-ROUTE-LICENSE-KEY-0001"
    assert data["email"] == "ops@example.invalid"


def test_activate_key_stdin_route_stores_key(tmp_path, monkeypatch):
    home = _activate_env(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO("STDIN-ROUTE-LICENSE-KEY-0001\n"))
    args = _parse_activate(["--key-stdin"])

    assert args.func(args) == 0

    assert _license_data(home)["key"] == "STDIN-ROUTE-LICENSE-KEY-0001"


def test_activate_prompt_route_uses_masked_prompt(tmp_path, monkeypatch):
    home = _activate_env(tmp_path, monkeypatch)
    from tokenpak.cli.commands import license_cmd

    prompts: list[str] = []

    def fake_getpass(prompt: str) -> str:
        prompts.append(prompt)
        return "PROMPT-ROUTE-LICENSE-KEY-0001"

    monkeypatch.setattr(license_cmd.getpass, "getpass", fake_getpass)
    args = _parse_activate(["--prompt-key"])

    assert args.func(args) == 0

    assert prompts == ["License key: "]
    assert _license_data(home)["key"] == "PROMPT-ROUTE-LICENSE-KEY-0001"


def test_activate_rejects_mixed_key_sources(tmp_path, monkeypatch, capsys):
    home = _activate_env(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO("STDIN-ROUTE-LICENSE-KEY-0002\n"))
    args = _parse_activate(["POSITIONAL-LICENSE-KEY-0002", "--key-stdin"])

    with pytest.raises(SystemExit) as exc:
        args.func(args)

    assert exc.value.code == 2
    assert "choose only one key source" in capsys.readouterr().err
    assert not (home / "license.json").exists()


def test_license_json_redacts_stored_key(tmp_path, monkeypatch, capsys):
    home = _activate_env(tmp_path, monkeypatch)
    activate_args = _parse_activate(["JSON-REDACTION-LICENSE-KEY-0001"])

    assert activate_args.func(activate_args) == 0
    capsys.readouterr()

    license_args = _cli_core.build_parser().parse_args(["license", "--json"])
    assert license_args.func(license_args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["has_key"] is True
    assert payload["license_path"] == str(home / "license.json")
    assert "key" not in payload
    assert "JSON-REDACTION-LICENSE-KEY-0001" not in json.dumps(payload)
