# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from argparse import Namespace

import tokenpak.cli.request_explorer as request_explorer
from tokenpak import _cli_core


def test_last_parser_dispatches_to_recent_request_command():
    parser = _cli_core.build_parser()
    args = parser.parse_args(["last", "--limit", "5"])

    assert args.limit == 5
    assert args.func is _cli_core.cmd_last


def test_last_prints_recent_non_2xx_diagnostics(monkeypatch, capsys):
    seen: dict[str, int] = {}

    def fake_load_requests(limit=None):
        seen["limit"] = limit
        return [
            {
                "id": "req-rate-limit",
                "timestamp": "2026-06-29T10:00:00Z",
                "model": "claude-sonnet",
                "endpoint": "/v1/messages",
                "request_type": "chat",
                "status_code": 429,
                "input_tokens": 1200,
                "output_tokens": 0,
            },
            {
                "id": "req-upstream",
                "timestamp": "2026-06-29T10:01:00Z",
                "model": "gpt-4.1",
                "endpoint": "https://api.openai.com/v1/chat/completions",
                "request_type": "chat",
                "status_code": 502,
                "input_tokens": 900,
                "output_tokens": 0,
            },
        ]

    monkeypatch.setattr(request_explorer, "load_requests", fake_load_requests)

    rc = _cli_core.cmd_last(Namespace(limit=5, json=False, verbose=False))

    assert rc == 0
    assert seen == {"limit": 5}
    out = capsys.readouterr().out
    assert "TOKENPAK | Last 2 Requests" in out
    assert "HTTP 429" in out
    assert "provider/quota" in out
    assert "rate limit or quota" in out
    assert "HTTP 502" in out
    assert "provider/upstream" in out


def test_last_json_includes_route_and_diagnostic(monkeypatch, capsys):
    monkeypatch.setattr(
        request_explorer,
        "load_requests",
        lambda limit=None: [
            {
                "id": "req-auth",
                "timestamp": "2026-06-29T10:02:00Z",
                "model": "claude-sonnet",
                "endpoint": "/v1/messages",
                "request_type": "chat",
                "status_code": 403,
                "input_tokens": 200,
                "output_tokens": 0,
                "session_id": "sess-1",
            }
        ],
    )

    rc = _cli_core.cmd_last(Namespace(limit=1, json=True, verbose=False))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "tokenpak-last/v1"
    assert payload["count"] == 1
    row = payload["requests"][0]
    assert row["status"] == "error"
    assert row["status_code"] == 403
    assert row["route_class"] == "provider"
    assert "provider/auth" in row["diagnostic"]
    assert row["session_id"] == "sess-1"


def test_last_rejects_non_positive_limit(capsys):
    rc = _cli_core.cmd_last(Namespace(limit=0, json=False, verbose=False))

    assert rc == 2
    assert "Limit must be at least 1" in capsys.readouterr().err
