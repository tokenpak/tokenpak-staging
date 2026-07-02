# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from argparse import Namespace

from tokenpak import _cli_core


def test_proxy_next_steps_include_dashboard(capsys):
    _cli_core._print_proxy_next_steps(8766)

    out = capsys.readouterr().out

    assert "tokenpak status" in out
    assert "tokenpak dashboard" in out
    assert "watch live requests and savings" in out
    assert "tokenpak savings" in out


def test_start_success_mentions_dashboard_next_step(monkeypatch, tmp_path, capsys):
    calls: list[dict[str, object]] = []

    class GoodValidator:
        def validate(self, config):
            return []

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return FakeProc()

    health_calls = iter(
        [
            None,
            {"compilation_mode": "hybrid", "stats": {"requests": 0}},
        ]
    )

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TOKENPAK_PORT", "9999")
    monkeypatch.setattr("tokenpak.core.config_loader.load_config", lambda: {})
    monkeypatch.setattr("tokenpak.core.config_validator.ConfigValidator", GoodValidator)
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    monkeypatch.setattr(_cli_core, "_proxy_get", lambda *args, **kwargs: next(health_calls))

    rc = _cli_core.cmd_start(Namespace())

    out = capsys.readouterr().out
    assert rc is None
    assert calls
    assert "tokenpak dashboard" in out
    assert "watch live requests and savings" in out
    assert "tokenpak savings" in out
