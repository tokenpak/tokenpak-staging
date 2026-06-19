# SPDX-License-Identifier: Apache-2.0
"""Tests for Codex companion MCP registration config."""

from __future__ import annotations

from pathlib import Path

from tokenpak.companion.codex.mcp_config import get_env_vars
from tokenpak.companion.config import CompanionConfig


def test_get_env_vars_forwards_explicit_session_and_project(tmp_path):
    cfg = CompanionConfig(
        budget_daily_usd=1.25,
        profile="lean",
        journal_dir=tmp_path,
        session_id="sess-env",
        session_id_source="env",
        project_dir="/repo/project",
    )

    env = get_env_vars(cfg)

    assert env["TOKENPAK_COMPANION_SESSION_ID"] == "sess-env"
    assert env["TOKENPAK_COMPANION_PROJECT_DIR"] == "/repo/project"
    assert env["TOKENPAK_COMPANION_BUDGET"] == "1.25"
    assert env["TOKENPAK_COMPANION_PROFILE"] == "lean"
    assert env["TOKENPAK_COMPANION_JOURNAL_DIR"] == str(tmp_path)


def test_get_env_vars_does_not_persist_file_sourced_session(tmp_path):
    cfg = CompanionConfig(
        journal_dir=Path.home() / ".tokenpak" / "companion",
        session_id="sess-file",
        session_id_source="file",
        project_dir="/repo/project",
    )

    env = get_env_vars(cfg)

    assert "TOKENPAK_COMPANION_SESSION_ID" not in env
    assert env["TOKENPAK_COMPANION_PROJECT_DIR"] == "/repo/project"
