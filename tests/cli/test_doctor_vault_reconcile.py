# SPDX-License-Identifier: Apache-2.0
"""Tests for P3-DOCTOR-VAULT-VIEW-RECONCILE.

`tokenpak doctor` must distinguish the CLI-registered vault index
(``~/.tokenpak/index.json``) from the LIVE companion/proxy vault. A missing or
empty CLI index does NOT mean retrieval is broken when the running proxy has the
vault loaded — `tokenpak claude` retrieves Paks through that proxy. The doctor
must report both states truthfully and never imply retrieval is broken when the
live proxy vault is loaded. Display-only; no behaviour change.
"""

from __future__ import annotations

import io
import json
import types
import urllib.request

from tokenpak.cli import cli_doctor


def _args():
    return types.SimpleNamespace(fix=False)


def _home(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_doctor.Path, "home", lambda *a, **k: tmp_path)


def _fake_health(monkeypatch, vault_index):
    payload = {"compilation_mode": "skeleton", "vault_index": vault_index}

    def _urlopen(url, timeout=2):  # noqa: ARG001
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)


def _proxy_down(monkeypatch):
    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)


def test_cli_index_missing_but_live_proxy_vault_loaded_is_informational(
    tmp_path, monkeypatch, capsys
):
    """CLI index absent + live proxy vault loaded → informational, NOT a broken-retrieval warning."""
    _home(monkeypatch, tmp_path)
    _fake_health(monkeypatch, {"available": True, "blocks": 10279})

    cli_doctor.cmd_doctor(_args())
    out = capsys.readouterr().out

    # The two-state distinction is surfaced: live proxy vault reported as loaded.
    assert "companion proxy vault loaded — 10279 blocks" in out
    # And the misleading "not available" warning is NOT emitted for the vault line.
    assert "Vault index         " in out
    assert "not available" not in out


def test_cli_index_missing_and_proxy_down_warns_honestly(tmp_path, monkeypatch, capsys):
    """CLI index absent + no live proxy → honest warning (nothing serves the vault)."""
    _home(monkeypatch, tmp_path)
    _proxy_down(monkeypatch)

    cli_doctor.cmd_doctor(_args())
    out = capsys.readouterr().out

    assert "Vault index         " in out
    assert "not available" in out


def test_cli_index_present_reports_blocks_regardless_of_proxy(tmp_path, monkeypatch, capsys):
    """A populated CLI index still reports its block count (regression)."""
    tdir = tmp_path / ".tokenpak"
    tdir.mkdir(parents=True)
    (tdir / "index.json").write_text(json.dumps({"blocks": [{"id": "a"}, {"id": "b"}]}))
    _home(monkeypatch, tmp_path)
    _proxy_down(monkeypatch)  # proxy state irrelevant when the CLI index is populated

    cli_doctor.cmd_doctor(_args())
    out = capsys.readouterr().out

    assert "Vault index" in out
    assert "2 blocks" in out
    assert "companion proxy vault loaded" not in out
