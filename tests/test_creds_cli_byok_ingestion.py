# SPDX-License-Identifier: Apache-2.0
"""BYOK ``tokenpak creds add`` secret-ingestion safety (argv-exposure fix).

A secret passed literally via ``--key``/``--token`` lands in shell history
and ``/proc/<pid>/cmdline``. These tests exercise the safe non-interactive
routes added for compat-first remediation (``--key-stdin``/``--token-stdin``,
``--key-file``/``--token-file``, ``TOKENPAK_CRED_SECRET``) and assert the
warning fires for the literal route — while never echoing the secret value.
"""

from __future__ import annotations

import io

import pytest

from tokenpak.creds import cli


@pytest.fixture
def captured_entry(monkeypatch):
    """Isolate cmd_add from disk + provider discovery; capture stored entry."""
    box: dict = {}

    def fake_add(cred_id, entry):
        box["id"] = cred_id
        box["entry"] = entry

    monkeypatch.setattr(cli.store, "add", fake_add)
    monkeypatch.setattr(cli, "discover_all", lambda: [])
    monkeypatch.delenv("TOKENPAK_CRED_SECRET", raising=False)
    return box


def _base_args(kind: str = "api_key") -> list[str]:
    return ["--id", "mykey", "--platform", "openai", "--kind", kind]


def test_literal_key_warns_but_stores(captured_entry, capsys):
    rc = cli.cmd_add(_base_args() + ["--key", "sk-SECRET-123"])
    assert rc == 0
    assert captured_entry["entry"]["key"] == "sk-SECRET-123"
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "shell history" in err
    # the secret value must never be echoed back
    assert "sk-SECRET-123" not in err


def test_key_stdin_route_no_warning(captured_entry, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("sk-STDIN-9\n"))
    rc = cli.cmd_add(_base_args() + ["--key-stdin"])
    assert rc == 0
    assert captured_entry["entry"]["key"] == "sk-STDIN-9"
    assert "WARNING" not in capsys.readouterr().err


def test_key_file_route_no_warning(captured_entry, capsys, tmp_path):
    f = tmp_path / "secret.txt"
    f.write_text("sk-FROMFILE\n")
    rc = cli.cmd_add(_base_args() + ["--key-file", str(f)])
    assert rc == 0
    assert captured_entry["entry"]["key"] == "sk-FROMFILE"
    assert "WARNING" not in capsys.readouterr().err


def test_env_route_no_warning(captured_entry, capsys, monkeypatch):
    monkeypatch.setenv("TOKENPAK_CRED_SECRET", "sk-FROMENV")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = cli.cmd_add(_base_args())
    assert rc == 0
    assert captured_entry["entry"]["key"] == "sk-FROMENV"
    assert "WARNING" not in capsys.readouterr().err


def test_bearer_token_literal_warns(captured_entry, capsys):
    rc = cli.cmd_add(_base_args(kind="bearer") + ["--token", "tok-SECRET"])
    assert rc == 0
    assert captured_entry["entry"]["token"] == "tok-SECRET"
    err = capsys.readouterr().err
    assert "--token" in err and "WARNING" in err
    assert "tok-SECRET" not in err


def test_stdin_supersedes_literal_but_still_warns(captured_entry, capsys, monkeypatch):
    # Safe route wins for the stored value, but a literal still present in
    # argv is exposed at the shell, so the warning must still fire.
    monkeypatch.setattr("sys.stdin", io.StringIO("sk-SAFE\n"))
    rc = cli.cmd_add(_base_args() + ["--key", "sk-EXPOSED", "--key-stdin"])
    assert rc == 0
    assert captured_entry["entry"]["key"] == "sk-SAFE"
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "sk-EXPOSED" not in err
    assert "sk-SAFE" not in err


def test_empty_stdin_route_fails_without_storing(captured_entry, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = cli.cmd_add(_base_args() + ["--key-stdin"])
    assert rc == 2
    assert "entry" not in captured_entry
