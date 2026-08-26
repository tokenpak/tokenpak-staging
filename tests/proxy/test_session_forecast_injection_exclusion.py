# SPDX-License-Identifier: Apache-2.0
"""End-to-end proof, with real proxy subprocesses and a stub upstream, that
the optional session-economics client-return decoration never enters
provider-bound bytes or accounting inputs — in EITHER the enabled or the
disabled state.

Two proxy subprocesses (one per state) drive the SAME two-turn conversation
against a stub upstream that always answers with the same fixed assistant
text. Turn 2's client request echoes back turn 1's assistant response
exactly as a real client would — decorated, in the enabled case. The proof:

  1. enabled -> the client's turn-1 response contains the marker; disabled
     -> it does not.
  2. the bytes the STUB UPSTREAM actually received for turn 2 are
     byte-IDENTICAL between the enabled and disabled runs — scrubbing
     perfectly cancels the decoration, so the provider sees the same
     conversation regardless of the flag.
  3. the ledger rows written for turn 2 are equal between both runs
     (ledger-equality) — the decoration changes no accounting input.
  4. re-sending the ALREADY-decorated turn-1 response as history a second
     time (replay) continues to scrub cleanly and stays upstream-byte-equal.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tests.proxy._proxy_subprocess import REPO_ROOT, ProxyProc, free_port
from tokenpak.proxy import session_forecast_injection as inj

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
_JSON_BODY = (_FIXTURES_DIR / "json_response_messages.json").read_bytes()
_ASSISTANT_TEXT = json.loads(_JSON_BODY)["content"][0]["text"]  # "Hello world"

_SESSION = "sedr-006-injection-session-1"


def _subprocess_extra_env(**extra: str) -> dict[str, str]:
    import os
    import site

    try:
        user_site = site.getusersitepackages()
    except Exception:
        user_site = ""
    path = str(REPO_ROOT)
    if user_site:
        path = f"{path}{os.pathsep}{user_site}"
    env = {"PYTHONPATH": path}
    env.update(extra)
    return env


class _RecordingUpstream(HTTPServer):
    """Stub upstream: always answers with the fixed fixture, records bodies."""

    def __init__(self, port: int) -> None:
        super().__init__(("127.0.0.1", port), _RecordingHandler)
        self.bodies: list[bytes] = []


class _RecordingHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        self.server.bodies.append(raw)  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_JSON_BODY)))
        self.end_headers()
        self.wfile.write(_JSON_BODY)


@pytest.fixture()
def upstream():
    server = _RecordingUpstream(free_port())
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server
    server.shutdown()
    t.join(timeout=2)


def _ledger_rows(db_path: Path) -> list[tuple]:
    """Accounting-relevant columns only — never id/timestamp, which are
    expected to differ between two independently-run subprocesses."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        return conn.execute(
            "SELECT model, input_tokens, output_tokens, estimated_cost, "
            "cache_read_tokens, cache_creation_tokens, stop_reason "
            "FROM requests ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()


def _turn1_and_extract(proxy: ProxyProc) -> str:
    """Send turn 1, return the exact client-facing assistant text
    (decorated or not, depending on the running proxy's flag)."""
    status, _, resp_body = proxy.post_message(
        "turn one", extra_headers={"X-TokenPak-Session": _SESSION}
    )
    assert status == 200
    assert proxy.wait_row_count(1) >= 1
    return json.loads(resp_body)["content"][0]["text"]


def test_disabled_state_is_marker_free_and_is_the_byte_baseline(upstream):
    stub_url = f"http://127.0.0.1:{upstream.server_address[1]}"
    proxy = ProxyProc(
        stub_url,
        extra_env=_subprocess_extra_env(TOKENPAK_SESSION_FORECAST_INJECTION="0"),
    )
    try:
        proxy.wait_ready()
        turn1_text = _turn1_and_extract(proxy)
        assert turn1_text == _ASSISTANT_TEXT
        assert not inj.contains_marker(turn1_text)

        turn2_messages = [
            {"role": "user", "content": "turn one"},
            {"role": "assistant", "content": turn1_text},
            {"role": "user", "content": "turn two"},
        ]
        status, _, _ = proxy.post_messages(
            turn2_messages, extra_headers={"X-TokenPak-Session": _SESSION}
        )
        assert status == 200
        assert proxy.wait_row_count(2) >= 2
        assert len(upstream.bodies) == 2
        # No marker was ever injected, so scrub is a pure identity pass —
        # the upstream sees exactly the client's own bytes.
        expected = json.dumps(
            {"model": "claude-sonnet-4-5", "max_tokens": 32, "messages": turn2_messages}
        ).encode()
        assert upstream.bodies[1] == expected
    finally:
        proxy.cleanup()


def test_enabled_state_decorates_client_but_stays_upstream_byte_equal_to_disabled(
    upstream,
):
    stub_url = f"http://127.0.0.1:{upstream.server_address[1]}"

    disabled = ProxyProc(
        stub_url,
        extra_env=_subprocess_extra_env(TOKENPAK_SESSION_FORECAST_INJECTION="0"),
    )
    enabled_upstream = _RecordingUpstream(free_port())
    t = threading.Thread(target=enabled_upstream.serve_forever, daemon=True)
    t.start()
    enabled = ProxyProc(
        f"http://127.0.0.1:{enabled_upstream.server_address[1]}",
        extra_env=_subprocess_extra_env(TOKENPAK_SESSION_FORECAST_INJECTION="1"),
    )
    try:
        disabled.wait_ready()
        enabled.wait_ready()

        disabled_turn1 = _turn1_and_extract(disabled)
        enabled_turn1 = _turn1_and_extract(enabled)

        # -- 1) the client-facing copies differ exactly by the marker --
        assert disabled_turn1 == _ASSISTANT_TEXT
        assert not inj.contains_marker(disabled_turn1)
        assert inj.contains_marker(enabled_turn1)
        assert inj.strip_markers(enabled_turn1) == _ASSISTANT_TEXT == disabled_turn1

        # -- 2) turn 2: each client echoes back EXACTLY what it received --
        disabled_turn2_messages = [
            {"role": "user", "content": "turn one"},
            {"role": "assistant", "content": disabled_turn1},
            {"role": "user", "content": "turn two"},
        ]
        enabled_turn2_messages = [
            {"role": "user", "content": "turn one"},
            {"role": "assistant", "content": enabled_turn1},
            {"role": "user", "content": "turn two"},
        ]
        status_d, _, _ = disabled.post_messages(
            disabled_turn2_messages, extra_headers={"X-TokenPak-Session": _SESSION}
        )
        status_e, _, _ = enabled.post_messages(
            enabled_turn2_messages, extra_headers={"X-TokenPak-Session": _SESSION}
        )
        assert status_d == 200 and status_e == 200
        assert disabled.wait_row_count(2) >= 2
        assert enabled.wait_row_count(2) >= 2

        # -- 3) byte-equality: the PROVIDER sees the identical turn-2 body,
        #       even though the client-echoed history differed by the marker.
        assert len(upstream.bodies) == 2
        assert len(enabled_upstream.bodies) == 2
        assert enabled_upstream.bodies[1] == upstream.bodies[1]
        assert inj.MARKER_OPEN_PREFIX.encode() not in enabled_upstream.bodies[1]

        # -- 4) ledger-equality: accounting inputs are unaffected --
        assert _ledger_rows(enabled.db_path) == _ledger_rows(disabled.db_path)

        # -- 5) replay stays clean: resend the decorated turn-1 text again
        #       as a THIRD-turn echo — scrub is idempotent across repeats.
        replay_messages = enabled_turn2_messages + [
            {"role": "assistant", "content": enabled_turn1},
            {"role": "user", "content": "turn three"},
        ]
        status_e2, _, _ = enabled.post_messages(
            replay_messages, extra_headers={"X-TokenPak-Session": _SESSION}
        )
        assert status_e2 == 200
        assert enabled.wait_row_count(3) >= 3
        assert len(enabled_upstream.bodies) == 3
        assert inj.MARKER_OPEN_PREFIX.encode() not in enabled_upstream.bodies[2]
    finally:
        disabled.cleanup()
        enabled.cleanup()
        enabled_upstream.shutdown()
        t.join(timeout=2)


def test_enabled_default_off_surface_is_still_byte_unchanged_without_session_header(
    upstream,
):
    """Even with the flag on, a request with no resolvable session id
    (default OSS surface, e.g. no X-TokenPak-Session header at all) must
    stay completely undecorated — never inject a marker without an
    unambiguous session identity."""
    stub_url = f"http://127.0.0.1:{upstream.server_address[1]}"
    proxy = ProxyProc(
        stub_url,
        extra_env=_subprocess_extra_env(TOKENPAK_SESSION_FORECAST_INJECTION="1"),
    )
    try:
        proxy.wait_ready()
        status, _, resp_body = proxy.post_message("no session header here")
        assert status == 200
        text = json.loads(resp_body)["content"][0]["text"]
        assert text == _ASSISTANT_TEXT
        assert not inj.contains_marker(text)
    finally:
        proxy.cleanup()
