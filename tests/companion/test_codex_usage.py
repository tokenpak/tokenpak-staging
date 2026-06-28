from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from tokenpak.companion.codex import usage

SALT = b"0" * 32


def _write_jsonl(path: Path, lines: list[object | str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for line in lines:
            if isinstance(line, str):
                fh.write(line + "\n")
            else:
                fh.write(json.dumps(line) + "\n")


def _token_event(last_total: int, cumulative_total: int = 100) -> dict:
    return {
        "timestamp": "2026-06-24T12:00:00Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "last_token_usage": {
                "input_tokens": last_total - 2,
                "cached_input_tokens": 1,
                "output_tokens": 2,
                "reasoning_output_tokens": 1,
                "total_tokens": last_total,
            },
            "total_token_usage": {
                "input_tokens": cumulative_total - 3,
                "cached_input_tokens": 10,
                "output_tokens": 3,
                "reasoning_output_tokens": 1,
                "total_tokens": cumulative_total,
            },
            "model_context_window": 200000,
            "rate_limits": {"primary": {"used": 1}},
        },
    }


def test_parse_session_jsonl_normalizes_token_count_events(tmp_path: Path):
    session = tmp_path / ".codex" / "sessions" / "2026" / "06" / "24" / "rollout.jsonl"
    _write_jsonl(
        session,
        [
            {"type": "event_msg", "payload": {"type": "something_else"}},
            _token_event(12, 120),
            '{"type":"event_msg","payload":{"type":"token_count",',
        ],
    )

    result = usage.parse_session_jsonl(session, salt=SALT)

    assert result["ok"] is True
    assert result["run_scope"] == "bounded"
    assert result["event_count"] == 1
    event = result["events"][0]
    assert event["incremental_model_call_usage"]["total_tokens"] == 12
    assert event["cumulative_session_usage"]["total_tokens"] == 120
    assert event["claim_eligibility"] == "spend_only"
    assert event["pricing"] == {"known": False, "reason": "unknown_billing_context"}
    assert event["rate_limits_present"] is True
    assert "malformed_jsonl" in {w["code"] for w in result["warnings"]}


def test_parse_session_jsonl_event_ids_include_ordinal(tmp_path: Path):
    session = tmp_path / ".codex" / "sessions" / "same-payload.jsonl"
    event = _token_event(9, 99)
    _write_jsonl(session, [event, event])

    result = usage.parse_session_jsonl(session, salt=SALT)

    ids = [event["source_event_id"] for event in result["events"]]
    assert len(ids) == 2
    assert ids[0] != ids[1]


def test_parse_session_jsonl_reports_no_token_count_events(tmp_path: Path):
    session = tmp_path / ".codex" / "sessions" / "empty.jsonl"
    _write_jsonl(session, [{"type": "event_msg", "payload": {"type": "other"}}])

    result = usage.parse_session_jsonl(session, salt=SALT)

    assert result["event_count"] == 0
    assert "no_token_count_events" in {w["code"] for w in result["warnings"]}


@pytest.mark.parametrize("name", ["auth.json", "history.jsonl", "config.toml", "codex-tui.log"])
def test_validate_session_path_rejects_forbidden_basenames(tmp_path: Path, name: str):
    forbidden = tmp_path / ".codex" / name
    forbidden.parent.mkdir(parents=True, exist_ok=True)
    forbidden.write_text("{}\n")

    with pytest.raises(usage.UsageSafetyError):
        usage.validate_session_path(forbidden)


def test_validate_session_path_rejects_symlink_to_forbidden_file(tmp_path: Path):
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text("{}\n")
    session_link = tmp_path / ".codex" / "sessions" / "safe-name.jsonl"
    session_link.parent.mkdir(parents=True, exist_ok=True)
    session_link.symlink_to(auth)

    with pytest.raises(usage.UsageSafetyError):
        usage.validate_session_path(session_link)


def test_latest_reports_ambiguity_for_recent_multiple_sessions(tmp_path: Path):
    sessions = tmp_path / ".codex" / "sessions" / "2026" / "06" / "24"
    first = sessions / "a.jsonl"
    second = sessions / "b.jsonl"
    _write_jsonl(first, [_token_event(1)])
    _write_jsonl(second, [_token_event(2)])
    os.utime(first, (1000, 1000))
    os.utime(second, (1010, 1010))

    selection = usage.select_latest_session(tmp_path / ".codex", ambiguity_window_s=300)

    assert selection.path is None
    assert selection.run_scope == "ambiguous"
    assert selection.warnings[0]["code"] == "ambiguous_latest_session"


def test_parse_result_does_not_expose_raw_path(tmp_path: Path):
    session = tmp_path / ".codex" / "sessions" / "sensitive-session-id.jsonl"
    _write_jsonl(session, [_token_event(7)])

    result_json = json.dumps(usage.parse_session_jsonl(session, salt=SALT))

    assert str(session) not in result_json
    assert "sensitive-session-id" not in result_json
    assert "source_fingerprint" in result_json


def test_capture_codex_exec_preserves_stdout_and_writes_sidecar(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / ".tpk"))
    sidecar = tmp_path / "sidecar.json"
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "thread_1"}) + "\n",
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 4,
                    "output_tokens": 3,
                    "reasoning_output_tokens": 1,
                },
            }
        ) + "\n",
    ]
    seen: dict[str, list[str]] = {}

    class FakeProcess:
        def __init__(self, cmd, **kwargs):
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs
            self.stdout = io.StringIO("".join(lines))

        def wait(self):
            return 0

    out = io.StringIO()
    return_code, written = usage.capture_codex_exec(
        ["codex", "exec", "hello"],
        sidecar=sidecar,
        stdout=out,
        popen_factory=FakeProcess,
        salt=SALT,
        codex_version="codex 0.test",
    )

    assert return_code == 0
    assert written == sidecar
    assert seen["cmd"] == ["codex", "exec", "--json", "hello"]
    assert out.getvalue() == "".join(lines)

    artifact = json.loads(sidecar.read_text())
    assert artifact["artifact_kind"] == "codex_exec_usage_sidecar"
    assert artifact["run_scope"] == "proven"
    assert artifact["claim_eligibility"] == "spend_only"
    assert artifact["pricing"] == {"known": False, "reason": "unknown_billing_context"}
    assert artifact["command_redacted"] is True
    assert artifact["codex_cli_version"] == "codex 0.test"
    assert artifact["event_count"] == 1
    assert artifact["events"][0]["incremental_model_call_usage"]["total_tokens"] == 13
    assert artifact["events"][0]["incremental_model_call_usage"]["input_tokens"] == 10
