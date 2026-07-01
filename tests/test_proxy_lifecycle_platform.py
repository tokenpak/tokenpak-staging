# SPDX-License-Identifier: Apache-2.0
"""Platform fallback tests for proxy lifecycle CLI helpers."""

from __future__ import annotations

import types

import tokenpak._cli_core as cli_core


def test_cmd_logs_reads_tempdir_proxy_log(monkeypatch, tmp_path, capsys):
    """The legacy fallback log path follows the platform temp directory."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli_core.tempfile, "gettempdir", lambda: str(tmp_path))
    log_path = tmp_path / "tokenpak-proxy.log"
    log_path.write_text("first\nsecond\n", encoding="utf-8")

    cli_core.cmd_logs(types.SimpleNamespace(lines=1))

    assert capsys.readouterr().out.strip() == "second"
