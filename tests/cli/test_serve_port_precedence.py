# SPDX-License-Identifier: Apache-2.0
"""`tokenpak serve` must honor the configured port when `--port` is absent.

`p_serve` declared ``--port`` with ``default=8766`` — an always-truthy
literal — so ``cmd_serve`` could never tell "the user passed nothing" from
"the user passed 8766". Both ``TOKENPAK_PORT`` and the config file's `port`
key were unreachable: ``cmd_serve`` always forwarded the literal 8766 to
``start_proxy``, and ``ProxyServer``'s own ``port or
os.environ.get("TOKENPAK_PORT", "8766")`` fallback in
``tokenpak/proxy/server.py`` never got a chance to run because a port was
always already supplied.

This mirrors ``tests/cli/test_start_port_flag.py`` (the equivalent, already
fixed, defect on ``tokenpak start``), extended with the config-file step
that ``serve`` also needs to honor.
"""

from __future__ import annotations

import pytest

from tokenpak._cli_core import _resolve_serve_port, build_parser


class TestServeParserDefault:
    def test_flag_is_captured(self):
        """--port reaches args, so cmd_serve can prefer it."""
        args = build_parser().parse_args(["serve", "--port", "8799"])
        assert args.port == 8799

    def test_default_is_none_not_8766(self):
        """The default must stay None.

        This is the load-bearing part. With a concrete 8766 default, an
        unflagged run is indistinguishable from `--port 8766`, so
        `_resolve_serve_port` would always take the flag branch and never
        consult `TOKENPAK_PORT` or the config file.
        """
        args = build_parser().parse_args(["serve"])
        assert args.port is None


class TestResolveServePortPrecedence:
    """Direct coverage of `_resolve_serve_port`'s precedence chain."""

    def test_flag_only(self, monkeypatch):
        monkeypatch.delenv("TOKENPAK_PORT", raising=False)
        assert _resolve_serve_port(8799) == 8799

    def test_env_only(self, monkeypatch):
        monkeypatch.setenv("TOKENPAK_PORT", "8777")
        assert _resolve_serve_port(None) == 8777

    def test_config_only(self, monkeypatch):
        monkeypatch.delenv("TOKENPAK_PORT", raising=False)
        monkeypatch.setattr(
            "tokenpak.core.config_loader.get",
            lambda key, default=None, *a, **kw: 19566 if key == "port" else default,
        )
        assert _resolve_serve_port(None) == 19566

    def test_flag_beats_env(self, monkeypatch):
        monkeypatch.setenv("TOKENPAK_PORT", "8777")
        assert _resolve_serve_port(8799) == 8799

    def test_env_beats_config(self, monkeypatch):
        monkeypatch.setenv("TOKENPAK_PORT", "8777")
        monkeypatch.setattr(
            "tokenpak.core.config_loader.get",
            lambda key, default=None, *a, **kw: 19566 if key == "port" else default,
        )
        assert _resolve_serve_port(None) == 8777

    def test_default_8766_when_none_configured(self, monkeypatch):
        monkeypatch.delenv("TOKENPAK_PORT", raising=False)
        monkeypatch.setattr(
            "tokenpak.core.config_loader.get",
            lambda key, default=None, *a, **kw: default,
        )
        assert _resolve_serve_port(None) == 8766

    def test_invalid_env_value_raises_clear_error(self, monkeypatch):
        monkeypatch.setenv("TOKENPAK_PORT", "not-a-port")
        with pytest.raises(ValueError, match="TOKENPAK_PORT"):
            _resolve_serve_port(None)

    def test_blank_env_value_falls_through_to_config(self, monkeypatch):
        """An empty/whitespace TOKENPAK_PORT is treated as unset, not garbage."""
        monkeypatch.setenv("TOKENPAK_PORT", "   ")
        monkeypatch.setattr(
            "tokenpak.core.config_loader.get",
            lambda key, default=None, *a, **kw: 19566 if key == "port" else default,
        )
        assert _resolve_serve_port(None) == 19566


class TestCmdServeSurfacesInvalidPortError:
    def test_cmd_serve_returns_failure_on_invalid_env_port(self, monkeypatch, capsys):
        import argparse

        from tokenpak._cli_core import cmd_serve

        monkeypatch.setenv("TOKENPAK_PORT", "garbage")
        args = argparse.Namespace(
            port=None,
            telemetry=False,
            ingest=False,
            workers=1,
            shutdown_timeout=None,
            safe=False,
            profile=None,
            stats_footer=False,
        )
        rc = cmd_serve(args)
        assert rc == 1
        assert "TOKENPAK_PORT" in capsys.readouterr().out
