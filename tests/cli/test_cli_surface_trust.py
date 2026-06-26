# SPDX-License-Identifier: Apache-2.0
"""CLI surface/trust regression tests.

Covers:
- Unknown-command nonzero exit (P1)
- Unknown-command + --help nonzero exit (P1)
- Version command uses /health, not /version (P1)
- 99.0.0 lockfile sentinel is labeled as a dev placeholder (P1)
- Help count is truthful (non-zero) (P2)
- Mission alias de-advertisement: dead aliases exit 2 with a redirect hint (P2)

These are offline tests (live_api_allowed: false).  The version-probe tests
mock urllib so no real network call is made.
"""

from __future__ import annotations

import sys
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Run the CLI dispatcher with argv; return (exit_code, stdout, stderr)."""
    from tokenpak.cli._impl import main

    out, err = StringIO(), StringIO()
    with patch("sys.stdout", out), patch("sys.stderr", err):
        with patch.object(sys, "argv", ["tokenpak"] + argv):
            try:
                main()
                code = 0
            except SystemExit as exc:
                code = int(exc.code) if exc.code is not None else 0
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# P1 — Unknown command nonzero exit
# ---------------------------------------------------------------------------


def test_unknown_command_exits_nonzero():
    """An unrecognised verb must exit 2, not 0."""
    code, _, _ = _run(["zzz-not-a-real-command"])
    assert code == 2


def test_unknown_command_help_flag_exits_nonzero():
    """tokenpak <unknown> --help must exit nonzero (was rc0 in 1.9.3)."""
    code, _, err = _run(["zzz-not-a-real-command", "--help"])
    assert code == 2
    assert "Unknown command" in err


def test_unknown_command_prints_to_stderr():
    """Error message must go to stderr, not stdout."""
    _, out, err = _run(["totally-made-up-verb"])
    assert "Unknown command" in err
    assert "Unknown command" not in out


# ---------------------------------------------------------------------------
# P1 — Version command probes /health, not /version
# ---------------------------------------------------------------------------


def _health_response() -> bytes:
    import json

    return json.dumps(
        {
            "status": "ok",
            "version": "1.9.3",
            "uptime_seconds": 3600,
            "requests_total": 42,
        }
    ).encode()


def test_version_uses_health_endpoint():
    """`tokenpak version` must probe /health and report the live version."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = _health_response()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        code, out, _ = _run(["version"])

    # Verify /health was the endpoint (not /version)
    called_urls = [str(call.args[0]) for call in mock_open.call_args_list]
    assert any("/health" in url for url in called_urls), (
        f"Expected /health to be probed, got: {called_urls}"
    )
    assert all("/version" not in url for url in called_urls), (
        "/version endpoint must not be called (returns 404 on live proxy)"
    )
    assert code == 0


def test_version_shows_proxy_reachable_when_healthy():
    """When /health returns 200, `tokenpak version` must not show 'not reachable'."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = _health_response()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        _, out, _ = _run(["version"])

    assert "not reachable" not in out
    assert "1.9.3" in out


def test_version_lockfile_sentinel_labeled():
    """A lockfile proxyVersion of 99.0.0 must be flagged as a dev sentinel."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = _health_response()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    sentinel_lock = '{"proxyVersion": "99.0.0", "configHash": "sha256:abc123"}'

    with (
        patch("urllib.request.urlopen", return_value=mock_resp),
        patch("tokenpak.cli._impl._LOCK_FILE") as mock_lock,
    ):
        mock_lock.exists.return_value = True
        mock_lock.read_text.return_value = sentinel_lock
        _, out, _ = _run(["version"])

    assert "99.0.0" in out
    assert "sentinel" in out.lower() or "dev" in out.lower()


# ---------------------------------------------------------------------------
# P2 — Help count is truthful
# ---------------------------------------------------------------------------


def test_help_all_lists_nonzero_commands():
    """`tokenpak help --all` must list at least one command (not 0)."""
    code, out, _ = _run(["help", "--all"])
    assert code == 0
    # Either the registry-based output lists commands, or the static fallback does
    assert len(out.strip()) > 0
    # Must not claim "0 commands"
    assert "0 commands" not in out


def test_help_default_lists_commands():
    """`tokenpak help` must show at least one command name."""
    code, out, _ = _run(["help"])
    assert code == 0
    assert len(out.strip()) > 0


# ---------------------------------------------------------------------------
# P2 — Mission alias de-advertisement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "alias,expected_hint_fragment",
    [
        ("pack", "compress"),
        ("reuse", "recipe"),
        ("guard", "budget"),
        ("verify", "prove"),
        ("receipt", "dispatch receipt"),
        ("cache", "stats"),
        ("sessions", "status"),
    ],
)
def test_mission_alias_exits_nonzero_with_redirect(alias, expected_hint_fragment):
    """Dead mission aliases must exit 2 and print a redirect hint."""
    code, _, err = _run([alias])
    assert code == 2, f"`tokenpak {alias}` should exit 2, got {code}"
    combined = err  # hints are on stderr
    assert expected_hint_fragment in combined, (
        f"`tokenpak {alias}` hint should mention '{expected_hint_fragment}', got:\n{combined}"
    )
