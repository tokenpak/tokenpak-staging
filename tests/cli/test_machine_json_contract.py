"""Machine-JSON output contract for clean-home CLI runs.

Covers parent gate ``p0-cli-trust-readiness-release-blockers-2026-06-22``
(P0-A pure JSON contract, P0-E no human tone in machine output) for the
``status --json`` and ``preview --json`` surfaces.

The bug these guard against only manifests on a *first run*: the top-level
dispatcher printed a human "Welcome to TokenPak!" block to stdout before the
command's JSON, so the machine stream began with prose instead of ``{``. The
pre-existing JSON tests reused the runner's ``$HOME`` (which already had the
first-run flag set), so they never exercised the contaminated path. Each test
here pins an isolated ``HOME``/``TOKENPAK_HOME`` so the first-run path is live.
"""

import json
import os
import subprocess
import sys

import pytest

# Point the proxy lookup at an almost-certainly-unused port so health/stats
# fetches fail fast (connection refused returns immediately) instead of
# depending on — or hanging on — a live proxy. The JSON contract holds either
# way; this just keeps the tests hermetic and quick.
_DEAD_PORT = "59231"

_WELCOME_MARKER = "Welcome to TokenPak"


def _run_cli(args, home):
    """Run ``tokenpak <args>`` with an isolated first-run HOME.

    Returns the completed process. ``HOME`` and ``TOKENPAK_HOME`` are pinned to
    a fresh directory so ``_is_first_run()`` is True for the run.
    """
    env = {
        **os.environ,
        "HOME": str(home),
        "TOKENPAK_HOME": str(home),
        "TOKENPAK_PORT": _DEAD_PORT,
    }
    return subprocess.run(
        [sys.executable, "-m", "tokenpak", *args],
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["status", "--json"], id="status-json"),
        pytest.param(["preview", "hello world", "--json"], id="preview-json"),
    ],
)
def test_machine_json_is_single_clean_document(args, tmp_path):
    """Clean-home ``--json`` stdout is exactly one parseable JSON document."""
    result = _run_cli(args, tmp_path)

    assert result.returncode == 0, result.stderr
    # First byte must open a JSON document — no welcome/prose preamble.
    assert result.stdout[:1] in ("{", "["), repr(result.stdout[:80])
    # The whole stream parses as a single JSON value (trailing prose would raise).
    json.loads(result.stdout)


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["status", "--json"], id="status-json"),
        pytest.param(["preview", "hello world", "--json"], id="preview-json"),
    ],
)
def test_machine_json_carries_no_human_prose(args, tmp_path):
    """No first-run welcome prose leaks to stdout or stderr in machine mode."""
    result = _run_cli(args, tmp_path)

    assert _WELCOME_MARKER not in result.stdout
    assert _WELCOME_MARKER not in result.stderr
    # Machine stderr must stay quiet by default (no first-run/progress prose).
    assert result.stderr == ""


def test_status_json_omits_meme_lines(tmp_path):
    """``status --json`` must not emit human-tone meme/tagline content (P0-E)."""
    result = _run_cli(["status", "--json"], tmp_path)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "meme_lines" not in payload


def test_machine_mode_does_not_consume_first_run_flag(tmp_path):
    """A machine run leaves the first-run flag unset so the human is still greeted."""
    # First invocation is machine-mode: must not print or consume the welcome.
    machine = _run_cli(["status", "--json"], tmp_path)
    assert _WELCOME_MARKER not in machine.stdout

    # A subsequent human invocation in the same HOME still shows the welcome.
    human = _run_cli(["status"], tmp_path)
    assert _WELCOME_MARKER in human.stdout


def test_human_first_run_still_greets(tmp_path):
    """Non-machine commands retain the human first-run welcome."""
    result = _run_cli(["status"], tmp_path)
    assert _WELCOME_MARKER in result.stdout
