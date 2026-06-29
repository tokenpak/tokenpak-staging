# SPDX-License-Identifier: Apache-2.0
"""Regression guards for companion visible surfaces.

These tests assert the shipped guard surfaces remain wired. They do not add or
restore feature behavior; they turn accidental deletion into a test failure.
"""

from __future__ import annotations

from pathlib import Path

from tokenpak.companion.codex import statusline_config as codex_status

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PAKLINE = _REPO_ROOT / "tokenpak" / "companion" / "statusline" / "pakline.sh"


def test_codex_native_status_items_cover_title_context_and_activity():
    assert "thread-title" in codex_status.DEFAULT_TITLE_ITEMS
    assert "task-progress" in codex_status.DEFAULT_TITLE_ITEMS

    status_items = set(codex_status.DEFAULT_STATUS_ITEMS)
    assert {"context-remaining", "context-used", "context-window-size"} <= status_items
    assert "task-progress" in status_items


def test_pakline_sources_task_cost_context_and_activity_fields():
    text = _PAKLINE.read_text(encoding="utf-8")
    assert "titles/${SESSION_ID}" in text
    assert "total_cost_usd" in text
    assert "exceeds_200k_tokens" in text
    assert "total_duration_ms" in text


def test_statusline_sources_are_present():
    assert _PAKLINE.exists()
    assert (_REPO_ROOT / "tokenpak" / "companion" / "hooks" / "pre_send.sh").exists()
    assert (_REPO_ROOT / "tokenpak" / "companion" / "codex" / "statusline_config.py").exists()
