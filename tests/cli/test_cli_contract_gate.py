# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import sys
from argparse import Namespace

import pytest

from tokenpak import _cli_core


def _invoke_main(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(_cli_core, "_NO_TUI_FLAG", False)
    monkeypatch.setattr(_cli_core, "_is_first_run", lambda: False)
    try:
        _cli_core.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_global_json_before_subcommand_sets_json_mode(monkeypatch, capsys):
    seen: dict[str, object] = {}

    def fake_status(args):
        seen["as_json"] = getattr(args, "as_json", False)
        print(json.dumps({"ok": True}))
        return 0

    monkeypatch.setattr(_cli_core, "cmd_status", fake_status)

    rc = _invoke_main(monkeypatch, ["tokenpak", "--json", "status"])

    assert rc == 0
    assert seen == {"as_json": True}
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_global_quiet_verbose_and_config_before_subcommand(monkeypatch, tmp_path):
    seen: dict[str, object] = {}
    status_calls: list[dict[str, object]] = []

    def fake_status(args):
        status_calls.append(
            {
                "quiet": getattr(args, "quiet", False),
                "config": getattr(args, "config", None),
            }
        )
        return 0

    def fake_doctor(args):
        seen["doctor_verbose"] = getattr(args, "verbose", False)
        return 0

    monkeypatch.setattr(_cli_core, "cmd_status", fake_status)
    monkeypatch.setattr(_cli_core, "cmd_doctor", fake_doctor)
    cfg = tmp_path / "tokenpak.config.json"

    assert _invoke_main(monkeypatch, ["tokenpak", "--quiet", "status"]) == 0
    assert _invoke_main(monkeypatch, ["tokenpak", "--config", str(cfg), "status"]) == 0
    assert _invoke_main(monkeypatch, ["tokenpak", "--verbose", "doctor"]) == 0

    assert status_calls[0]["quiet"] is True
    assert status_calls[1]["config"] == str(cfg)
    assert seen["doctor_verbose"] is True


def test_start_uses_cli_port_for_background_proxy(monkeypatch, tmp_path):
    calls: list[dict[str, object]] = []

    class GoodValidator:
        def validate(self, config):
            return []

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return FakeProc()

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TOKENPAK_PORT", raising=False)
    monkeypatch.setattr("tokenpak.core.config_loader.load_config", lambda: {})
    monkeypatch.setattr("tokenpak.core.config_validator.ConfigValidator", GoodValidator)
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    monkeypatch.setattr(_cli_core, "_proxy_get", lambda *args, **kwargs: None)

    rc = _cli_core.cmd_start(Namespace(port=9999, log_level="debug"))

    assert rc is None
    assert calls
    assert calls[0]["env"]["TOKENPAK_PORT"] == "9999"
    assert calls[0]["env"]["TOKENPAK_LOG_LEVEL"] == "debug"
    assert calls[0]["cmd"][-4:] == ["--port", "9999", "--log-level", "debug"]


def test_start_invalid_config_returns_config_error(monkeypatch, capsys):
    class BadValidator:
        def validate(self, config):
            return ["bad config"]

    monkeypatch.setattr("tokenpak.core.config_loader.load_config", lambda: {})
    monkeypatch.setattr("tokenpak.core.config_validator.ConfigValidator", BadValidator)

    rc = _cli_core.cmd_start(Namespace(port=8766, log_level="info"))

    assert rc == 4
    assert "Config validation failed" in capsys.readouterr().err


def test_stop_missing_pid_returns_user_error(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    rc = _cli_core.cmd_stop(Namespace())

    assert rc == 1


@pytest.mark.parametrize("verb", ["audit", "compliance", "watch"])
def test_stub_commands_return_nonzero(verb):
    parser = _cli_core.build_parser()
    args = parser.parse_args([verb])

    assert args.func(args) == 1


def test_unknown_command_help_is_usage_error(monkeypatch, capsys):
    rc = _invoke_main(monkeypatch, ["tokenpak", "not-a-command", "--help"])

    assert rc == 2
    captured = capsys.readouterr()
    assert "Unknown command" in captured.err
    assert "no additional help available" not in captured.out


def test_unknown_command_suggestions_use_real_commands(monkeypatch, capsys):
    rc = _invoke_main(monkeypatch, ["tokenpak", "compres"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "tokenpak compress" in err
    assert "tokenpak creds" not in err


def test_low_confidence_unknown_command_suppresses_suggestion(monkeypatch, capsys):
    rc = _invoke_main(monkeypatch, ["tokenpak", "xqznotclose"])

    assert rc == 2
    assert "Did you mean" not in capsys.readouterr().err


def test_tier1_cli_contract_commands_are_exposed(capsys):
    parser = _cli_core.build_parser()
    invokable = _cli_core.registered_command_names(parser)
    canonical = {
        "serve",
        "integrate",
        "cost",
        "savings",
        "demo",
        "doctor",
        "status",
        "creds",
        "cache",
        "index",
        "replay",
    }

    assert canonical <= invokable

    _cli_core._print_quick_help()
    out = capsys.readouterr().out
    for command in canonical:
        assert f"  {command:<12}" in out
