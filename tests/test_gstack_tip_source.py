# SPDX-License-Identifier: Apache-2.0
"""GstackTIPSource tests — first ExternalToolTIPSource instance (Std 23 §9).

Covers the packet acceptance criteria for the concrete adapter:

- AC #1 version-tolerant parsing: unknown shapes skip + structured log,
  corrupt lines never crash, NO hardcoded role/phase enumeration
- AC #2 on-demand batch over the transcript, off-by-default
- AC #3/§9.3 records are TokenPak-observed, raw counts only
- AC #4 flag-on run over a fixture produces ext.gstack.* records with
  detected role + phase + observed token counters, surfaced via the
  `tokenpak tip` CLI
- AC #6 read-only: transcript + state files byte-identical after a run
"""

from __future__ import annotations

import argparse
import json
import re

import pytest

from tokenpak.sources.external_tool_tip import ENV_FLAG, EXT_LABEL_RE
from tokenpak.sources.gstack_tip_source import (
    ENV_STATE_ROOT,
    GstackTIPSource,
    parse_session_events,
)

# ---------------------------------------------------------------------------
# Fixture transcript — a small gstack sprint inside a Claude Code session
# ---------------------------------------------------------------------------


def _jl(obj) -> str:
    return json.dumps(obj) + "\n"


def _user(text, ts=None):
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user", "content": text}}


def _assistant(usage, ts=None):
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": usage}}


def write_fixture_session(projects_root, lines, project="-home-user-proj",
                          session="abc123"):
    proj_dir = projects_root / project
    proj_dir.mkdir(parents=True, exist_ok=True)
    path = proj_dir / f"{session}.jsonl"
    path.write_text("".join(lines), encoding="utf-8")
    return path


@pytest.fixture()
def sprint_fixture(tmp_path):
    """Transcript with two gstack invocations + assistant usage in each span."""
    lines = [
        _jl(_user("hello, ordinary message", ts="2026-06-10T01:00:00Z")),
        _jl(_user("<command-name>/gstack:build</command-name> role=qa-lead",
                  ts="2026-06-10T01:01:00Z")),
        _jl(_assistant({"input_tokens": 100, "output_tokens": 40},
                       ts="2026-06-10T01:02:00Z")),
        _jl(_assistant({"input_tokens": 50, "output_tokens": 10,
                        "cache_read_input_tokens": 7},
                       ts="2026-06-10T01:03:00Z")),
        _jl(_user("/gstack:review phase: verify", ts="2026-06-10T01:04:00Z")),
        _jl(_assistant({"input_tokens": 20, "output_tokens": 5},
                       ts="2026-06-10T01:05:00Z")),
    ]
    root = tmp_path / "projects"
    path = write_fixture_session(root, lines)
    return root, path


# ---------------------------------------------------------------------------
# AC #4 — observable records with role + phase + observed counters
# ---------------------------------------------------------------------------


def test_gstack_sprint_emits_observed_records(sprint_fixture):
    root, _ = sprint_fixture
    records = GstackTIPSource(projects_root=root).collect()
    assert len(records) == 2

    build, review = records
    assert build.tool == "gstack"
    assert build.command == "build"
    assert build.role == "qa-lead"
    assert "ext.gstack.command.build" in build.labels
    assert "ext.gstack.role.qa-lead" in build.labels
    assert "ext.gstack.usage_observed" in build.labels
    assert "ext.gstack.cost_observed" in build.labels
    assert build.observed_usage["input_tokens"] == 150
    assert build.observed_usage["output_tokens"] == 50
    assert build.observed_usage["cache_read_input_tokens"] == 7
    assert build.observed_usage["assistant_messages"] == 2
    assert build.session_id == "abc123"
    assert build.first_timestamp == "2026-06-10T01:01:00Z"
    assert build.last_timestamp == "2026-06-10T01:03:00Z"

    assert review.command == "review"
    assert review.phase == "verify"
    assert "ext.gstack.phase.verify" in review.labels
    assert review.observed_usage["input_tokens"] == 20


def test_all_emitted_labels_are_ext_namespace(sprint_fixture):
    root, _ = sprint_fixture
    for record in GstackTIPSource(projects_root=root).collect():
        for label in record.labels:
            assert EXT_LABEL_RE.match(label), label
            assert label.startswith("ext.gstack."), label
            assert not label.startswith("tip."), label


def test_records_are_tokenpak_observed_never_tool_native(sprint_fixture):
    root, _ = sprint_fixture
    for record in GstackTIPSource(projects_root=root).collect():
        assert record.provenance["claim"] == "tokenpak-observed"
        assert record.provenance["tool_native_tip"] is False


# ---------------------------------------------------------------------------
# AC #1 — version tolerance, no enum, graceful degradation
# ---------------------------------------------------------------------------


def test_novel_phase_token_is_captured_dynamically(tmp_path):
    """A never-before-seen command token must work — no hardcoded enum."""
    root = tmp_path / "projects"
    write_fixture_session(root, [
        _jl(_user("/gstack:zzz-novel-phase.v9", ts="t1")),
        _jl(_assistant({"input_tokens": 1, "output_tokens": 1}, ts="t2")),
    ])
    records = GstackTIPSource(projects_root=root).collect()
    assert len(records) == 1
    assert records[0].command == "zzz-novel-phase.v9"
    assert "ext.gstack.command.zzz-novel-phase.v9" in records[0].labels


def test_malformed_command_shape_skips_with_log(tmp_path, caplog):
    root = tmp_path / "projects"
    write_fixture_session(root, [
        _jl(_user("/gstack:??? what even is this", ts="t1")),
        _jl(_assistant({"input_tokens": 1}, ts="t2")),
    ])
    with caplog.at_level("INFO"):
        records = GstackTIPSource(projects_root=root).collect()
    assert records == []
    assert any("unrecognized-command-shape" in r.message for r in caplog.records)


def test_corrupt_and_unknown_lines_never_crash(tmp_path):
    root = tmp_path / "projects"
    write_fixture_session(root, [
        "{not json at all\n",
        _jl({"type": "file-history-snapshot", "blob": "x"}),
        _jl({"type": "some-future-record-kind", "v": 99}),
        _jl(_user("/gstack:plan", ts="t1")),
        _jl({"type": "assistant", "message": "not-a-dict"}),
        _jl(_assistant({"input_tokens": "not-an-int", "output_tokens": 3}, ts="t2")),
        _jl([1, 2, 3]),
    ])
    records = GstackTIPSource(projects_root=root).collect()
    assert len(records) == 1
    assert records[0].command == "plan"
    # non-int counters tolerated (dropped), int ones kept
    assert records[0].observed_usage.get("output_tokens") == 3
    assert "input_tokens" not in records[0].observed_usage


def test_no_hardcoded_role_or_phase_enum_in_adapter():
    """Guard: adapter source must not enumerate gstack roles/phases."""
    from pathlib import Path

    import tokenpak.sources.gstack_tip_source as mod

    text = Path(mod.__file__).read_text(encoding="utf-8")
    # No list/set/tuple literal of known role or phase names.
    assert not re.search(
        r"(ROLES|PHASES|KNOWN_COMMANDS)\s*[:=]", text
    ), "adapter must stay enum-free (feedback_always_dynamic)"


def test_non_gstack_sessions_emit_nothing(tmp_path):
    root = tmp_path / "projects"
    write_fixture_session(root, [
        _jl(_user("just talking about gstack the project, no slash command")),
        _jl(_assistant({"input_tokens": 9, "output_tokens": 9})),
    ])
    assert GstackTIPSource(projects_root=root).collect() == []


def test_missing_projects_root_is_empty_not_error(tmp_path):
    root = tmp_path / "does-not-exist"
    assert GstackTIPSource(projects_root=root).collect() == []


# ---------------------------------------------------------------------------
# AC #6 — read-only with respect to gstack + transcripts
# ---------------------------------------------------------------------------


def test_collect_never_modifies_transcript_or_state(tmp_path, monkeypatch,
                                                    sprint_fixture):
    root, transcript = sprint_fixture
    state_dir = tmp_path / "gstack-state"
    state_dir.mkdir()
    state_file = state_dir / "sprint.json"
    state_file.write_text(json.dumps({"current": {"role": "builder"}}),
                          encoding="utf-8")
    monkeypatch.setenv(ENV_STATE_ROOT, str(state_dir))

    before_transcript = transcript.read_bytes()
    before_state = state_file.read_bytes()
    transcript_mtime = transcript.stat().st_mtime_ns
    state_mtime = state_file.stat().st_mtime_ns

    records = GstackTIPSource(projects_root=root).collect()
    assert records  # sanity: the run actually observed something

    assert transcript.read_bytes() == before_transcript
    assert state_file.read_bytes() == before_state
    assert transcript.stat().st_mtime_ns == transcript_mtime
    assert state_file.stat().st_mtime_ns == state_mtime
    # no new files dropped into either surface
    assert sorted(p.name for p in state_dir.iterdir()) == ["sprint.json"]


# ---------------------------------------------------------------------------
# GSTACK_STATE_ROOT enrichment (optional, tolerant)
# ---------------------------------------------------------------------------


def test_state_root_enriches_missing_role_and_phase(tmp_path):
    root = tmp_path / "projects"
    write_fixture_session(root, [
        _jl(_user("/gstack:qa", ts="t1")),
        _jl(_assistant({"input_tokens": 2, "output_tokens": 2}, ts="t2")),
    ])
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({"current": {"role": "qa-lead", "phase": "verify"}}),
        encoding="utf-8",
    )
    records = GstackTIPSource(
        projects_root=root, gstack_state_root=state_dir
    ).collect()
    assert records[0].role == "qa-lead"
    assert records[0].phase == "verify"
    assert "ext.gstack.role.qa-lead" in records[0].labels
    assert "ext.gstack.phase.verify" in records[0].labels


def test_inline_hints_win_over_state_root(tmp_path):
    root = tmp_path / "projects"
    write_fixture_session(root, [
        _jl(_user("/gstack:qa role=reviewer", ts="t1")),
        _jl(_assistant({"input_tokens": 2}, ts="t2")),
    ])
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({"role": "builder"}), encoding="utf-8",
    )
    records = GstackTIPSource(
        projects_root=root, gstack_state_root=state_dir
    ).collect()
    assert records[0].role == "reviewer"


def test_unparseable_state_files_degrade_gracefully(tmp_path, caplog):
    root = tmp_path / "projects"
    write_fixture_session(root, [
        _jl(_user("/gstack:qa", ts="t1")),
        _jl(_assistant({"input_tokens": 2}, ts="t2")),
    ])
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "broken.json").write_text("{nope", encoding="utf-8")
    with caplog.at_level("INFO"):
        records = GstackTIPSource(
            projects_root=root, gstack_state_root=state_dir
        ).collect()
    assert len(records) == 1
    assert records[0].role is None
    assert any("unparseable" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# parse_session_events — tolerant low-level parse
# ---------------------------------------------------------------------------


def test_parse_session_events_unreadable_file(tmp_path):
    assert parse_session_events(tmp_path / "missing.jsonl") == []


# ---------------------------------------------------------------------------
# AC #4 — CLI surface: `tokenpak tip observe` with the flag on
# ---------------------------------------------------------------------------


def test_cli_observe_flag_on_shows_ext_gstack_records(sprint_fixture,
                                                      monkeypatch, capsys):
    from tokenpak.cli.commands.tip import cmd_tip_observe

    root, _ = sprint_fixture
    monkeypatch.setenv(ENV_FLAG, "1")
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(root))
    monkeypatch.delenv(ENV_STATE_ROOT, raising=False)

    rc = cmd_tip_observe(argparse.Namespace(json=True, tool="gstack"))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] is False
    assert "gstack" in payload["sources"]
    assert len(payload["records"]) == 2
    labels = {label for r in payload["records"] for label in r["labels"]}
    assert any(label.startswith("ext.gstack.") for label in labels)
    assert all(not label.startswith("tip.") for label in labels)
    roles = {r["role"] for r in payload["records"]}
    assert "qa-lead" in roles
    assert payload["records"][0]["provenance"]["claim"] == "tokenpak-observed"


def test_cli_sources_lists_gstack_adapter(monkeypatch, capsys):
    from tokenpak.cli.commands.tip import cmd_tip_sources

    monkeypatch.delenv(ENV_FLAG, raising=False)
    rc = cmd_tip_sources(argparse.Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["enabled"] is False
    tools = {s["tool"] for s in payload["sources"]}
    assert "gstack" in tools
