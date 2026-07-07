"""Regression: `tokenpak monitor` is retired-first.

The static dashboard HTML that the old monitor server served was deleted, so
`tokenpak monitor` used to serve a misleading HTTP 200 "Dashboard not found"
page. Per the CLI UX deprecation rule (retire-first, no restore), `cmd_monitor`
must now print an honest deprecation notice pointing to `dashboard` / `status`
and exit WITHOUT starting a server. The verb stays reachable for one minor
version; hard removal is a separate version-floor note.
"""

from tokenpak import _cli_core


def _run_monitor(argv, monkeypatch):
    """Drive the real CLI parser for `monitor` and return the handler's rc.

    Guard: monitor must NOT start the retired dashboard server. If a regression
    re-adds the ``from ...monitoring.server import run; run(port=...)`` call, the
    patched ``run`` raises and the test fails.
    """
    import tokenpak.telemetry.monitoring.server as server_mod

    def _boom(*args, **kwargs):
        raise AssertionError("monitor must not start the retired dashboard server")

    monkeypatch.setattr(server_mod, "run", _boom)

    parser = _cli_core.build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def test_monitor_prints_deprecation_notice_and_exits(capsys, monkeypatch):
    rc = _run_monitor(["monitor"], monkeypatch)

    # None/0 both map to exit 0 in the dispatcher: a deprecated verb "still
    # works" — it is an honest no-op redirect, not an error.
    assert rc in (None, 0)

    err = capsys.readouterr().err
    assert "deprecated" in err.lower()
    # Points users at the live replacements.
    assert "tokenpak dashboard" in err
    assert "tokenpak status" in err


def test_monitor_port_flag_still_exits_gracefully(capsys, monkeypatch):
    # `--port` is accepted (ignored) so pre-existing invocations do not hit an
    # argparse usage error — they get the deprecation notice and exit cleanly.
    rc = _run_monitor(["monitor", "--port", "9000"], monkeypatch)

    assert rc in (None, 0)
    assert "deprecated" in capsys.readouterr().err.lower()
