# SPDX-License-Identifier: Apache-2.0
"""CLI debug receipt-view tests (AC-4).

The in-scope ``tokenpak.cli.commands.debug`` module renders a redaction-safe
Receipt v1 for a recorded request, or a support-bundle pointer when no request
is found. (Live exposure on the ``tokenpak debug`` parser is a separate
``_cli_core.py`` wiring step — out of this packet's scope.)
"""

from __future__ import annotations

import json
import sqlite3
import types


def _seed_request(monkeypatch, tmp_path) -> str:
    """Create an isolated monitor DB with one request row; return its id."""
    from tokenpak import _paths
    from tokenpak.cli.request_explorer import load_requests
    from tokenpak.proxy.monitor import Monitor

    home = tmp_path / "home"
    monkeypatch.setenv("TOKENPAK_HOME", str(home))
    monkeypatch.delenv("TOKENPAK_DB", raising=False)
    monkeypatch.delenv("TOKENPAK_MONITOR_DB", raising=False)

    # Isolate the debug-capture blob dir: it is derived from Path.home() at import,
    # so without this redirect captures would write to the real ~/.tokenpak/debug.
    from tokenpak.debug import capture as _capture

    blob_dir = tmp_path / "debug"
    blob_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_capture, "_BLOB_DIR", blob_dir)

    db = _paths.monitor_db(mode="write")
    Monitor(str(db))
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """
            INSERT INTO requests (
                timestamp, model, request_type, input_tokens, output_tokens,
                estimated_cost, latency_ms, status_code, endpoint,
                cache_read_tokens, cache_creation_tokens, would_have_saved,
                session_id, agent_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-25T18:05:00Z",
                "claude-sonnet",
                "chat",
                200,
                50,
                0.018,
                71,
                200,
                "/v1/messages",
                80,
                20,
                0.006,
                "sess-x",
                "proxy-test",
            ),
        )
        conn.commit()

    return str(load_requests()[0]["id"])


def test_receipt_view_renders_recorded_request(monkeypatch, tmp_path):
    from tokenpak.cli.commands.debug import _render_request_receipt

    rid = _seed_request(monkeypatch, tmp_path)
    payload = json.loads(_render_request_receipt(rid, redact=True))

    assert payload["schema_version"] == "receipt.v1"
    assert payload["receipt_id"] == f"rcpt_{rid}"
    assert payload["cost"]["estimated_cost_usd"] == {"available": True, "value": 0.018}
    assert payload["optimization"]["would_have_saved_usd"]["value"] == 0.006
    assert payload["context"]["cache_read_tokens"]["value"] == 80
    assert payload["trail"]["agent_id"]["value"] == "proxy-test"
    # No capture exists -> pointer present=False but reports the configured mode.
    assert payload["debug_pointer"]["present"] is False
    assert "path" not in payload["debug_pointer"]


def test_receipt_view_missing_request_returns_support_pointer(monkeypatch, tmp_path):
    from tokenpak.cli.commands.debug import _render_request_receipt

    _seed_request(monkeypatch, tmp_path)
    out = _render_request_receipt("does-not-exist", redact=True)
    assert "No receipt" in out
    assert "Debug capture bundle" in out
    assert "tokenpak debug list" in out


def test_receipt_view_no_id_returns_support_pointer(monkeypatch, tmp_path):
    from tokenpak.cli.commands.debug import _render_request_receipt

    _seed_request(monkeypatch, tmp_path)
    out = _render_request_receipt(None, redact=True)
    assert "No receipt" in out


def test_receipt_view_redacts_capture_path(monkeypatch, tmp_path):
    from tokenpak.cli.commands.debug import _render_request_receipt
    from tokenpak.debug import capture
    from tokenpak.debug.capture import CaptureMode

    rid = _seed_request(monkeypatch, tmp_path)

    # Write a hash-only capture keyed by the request id, so the pointer resolves.
    monkeypatch.setenv("TOKENPAK_DEBUG_CAPTURE", "hash_only")
    dest = capture.capture(
        rid, {"prompt": "secret"}, {"completion": "secret"}, mode=CaptureMode.HASH_ONLY
    )
    assert dest is not None

    redacted = _render_request_receipt(rid, redact=True)
    payload = json.loads(redacted)
    assert payload["debug_pointer"]["present"] is True
    assert payload["debug_pointer"]["capture_mode"] == "hash_only"
    assert "path" not in payload["debug_pointer"]
    # Redacted output never leaks the on-disk capture path or its directory.
    assert str(dest) not in redacted

    raw = _render_request_receipt(rid, redact=False)
    assert str(dest) in raw


def test_debug_cmd_routes_receipt(monkeypatch, tmp_path, capsys):
    from tokenpak.cli.commands.debug import debug_cmd

    rid = _seed_request(monkeypatch, tmp_path)
    args = types.SimpleNamespace(debug_args=["receipt", rid])
    debug_cmd(args)
    out = capsys.readouterr().out
    assert '"schema_version": "receipt.v1"' in out
    assert f"rcpt_{rid}" in out


def test_debug_cmd_usage_mentions_receipt(capsys):
    from tokenpak.cli.commands.debug import debug_cmd

    debug_cmd(types.SimpleNamespace(debug_args=[]))
    out = capsys.readouterr().out
    assert "receipt <request_id>" in out
