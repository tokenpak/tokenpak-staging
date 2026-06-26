# SPDX-License-Identifier: Apache-2.0
"""Regression coverage for public config-sync behavior."""

from __future__ import annotations

import json
import subprocess
import types


def test_config_sync_git_does_not_call_private_vault_script(monkeypatch, tmp_path, capsys):
    from tokenpak import _cli_core

    cfg = {"proxy": {"port": 8766}}
    cfg_path = tmp_path / "config.json"
    lock_path = tmp_path / "tokenpak.lock.json"
    cfg_path.write_text(json.dumps(cfg))
    lock_path.write_text(
        json.dumps({"configHash": _cli_core._compute_config_hash(cfg)})
    )

    monkeypatch.setattr(_cli_core, "_TOKENPAK_CFG", cfg_path)
    monkeypatch.setattr(_cli_core, "_LOCK_FILE", lock_path)

    def fail_run(*_args, **_kwargs):
        raise AssertionError("config sync must not execute host-private scripts")

    monkeypatch.setattr(subprocess, "run", fail_run)

    args = types.SimpleNamespace(source="git", dry_run=True)
    _cli_core.cmd_config_sync(args)

    out = capsys.readouterr().out
    assert "Git-backed config sync is not bundled in the public CLI" in out
    assert "Config matches lock" in out
