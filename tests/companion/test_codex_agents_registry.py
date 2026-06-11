# SPDX-License-Identifier: Apache-2.0
"""AGENTS.md tool list is generated from the MCP TOOLS registry.

The hand-maintained 7-tool list drifted when the registry grew to 9
tools (``vault_search`` / ``vault_retrieve`` were missing).  These tests
pin the fix: the "Available MCP tools" section is derived from the
registry, so any future registry change is reflected automatically and a
stale hand-list can never reappear.

Also covers the doctor's ``AGENTS.override.md`` shadowing check: an
override file that lacks the TokenPak section silently drops every
companion behavior rule, and the doctor must WARN about it.
"""
from __future__ import annotations

import re
from pathlib import Path

from tokenpak.companion.codex import agents_md, doctor
from tokenpak.companion.codex.agents_md import generate_agents_md, install_agents_md
from tokenpak.companion.mcp.tools import TOOLS

_REGISTRY_NAMES = [tool.name for tool in TOOLS]


def _tools_section(content: str) -> str:
    """Extract the bullet block under "## Available MCP tools"."""
    marker = "## Available MCP tools"
    start = content.index(marker) + len(marker)
    end = content.index("\n## ", start)
    return content[start:end]


def _bullet_names(section: str) -> "list[str]":
    return re.findall(r"^- \*\*([A-Za-z0-9_]+)\*\*", section, flags=re.MULTILINE)


# ---------------------------------------------------------------------------
# Tool list is registry-derived
# ---------------------------------------------------------------------------

def test_every_registry_tool_is_listed():
    section = _tools_section(generate_agents_md())
    for tool in TOOLS:
        assert f"- **{tool.name}** —" in section, f"{tool.name} missing from AGENTS.md"


def test_tool_bullet_count_matches_registry_exactly():
    names = _bullet_names(_tools_section(generate_agents_md()))
    assert len(names) == len(TOOLS)


def test_tool_list_order_matches_registry_order():
    names = _bullet_names(_tools_section(generate_agents_md()))
    assert names == _REGISTRY_NAMES


def test_vault_tools_are_included():
    """The original drift: vault_search + vault_retrieve absent from the hand list."""
    section = _tools_section(generate_agents_md())
    assert "- **vault_search** —" in section
    assert "- **vault_retrieve** —" in section


def test_no_unknown_tools_in_list():
    """Every bullet must name a registry tool (no stale hand-maintained rows)."""
    names = _bullet_names(_tools_section(generate_agents_md()))
    unknown = [n for n in names if n not in _REGISTRY_NAMES]
    assert not unknown, f"AGENTS.md lists tools not in the registry: {unknown}"


def test_guidance_backticked_tool_names_exist_in_registry():
    """Usage guidance may only reference tools the server actually exposes."""
    content = generate_agents_md()
    backticked = re.findall(r"`([A-Za-z0-9_]+)`", content)
    tool_like = [t for t in backticked if "_" in t]
    stale = [t for t in tool_like if t not in _REGISTRY_NAMES]
    assert not stale, f"guidance references unknown tools: {stale}"


def test_tool_summary_is_first_sentence():
    assert agents_md._tool_summary("First part. Second part.") == "First part."
    assert agents_md._tool_summary("Only one sentence.") == "Only one sentence."


def test_bullets_use_first_sentence_of_registry_description():
    section = _tools_section(generate_agents_md())
    for tool in TOOLS:
        summary = agents_md._tool_summary(tool.description)
        assert f"- **{tool.name}** — {summary}" in section


def test_content_is_module_constant():
    """Generation happens once at import; calls return the same object."""
    assert generate_agents_md() is agents_md._AGENTS_CONTENT


# ---------------------------------------------------------------------------
# install_agents_md round-trips the generated content
# ---------------------------------------------------------------------------

def test_install_writes_all_registry_tools(tmp_path: Path):
    path = install_agents_md(target=str(tmp_path))
    content = path.read_text()
    for name in _REGISTRY_NAMES:
        assert name in content


def test_install_is_idempotent(tmp_path: Path):
    first = install_agents_md(target=str(tmp_path)).read_text()
    second = install_agents_md(target=str(tmp_path)).read_text()
    assert first == second


def test_install_refreshes_stale_hand_list(tmp_path: Path):
    """A previously-installed 7-tool section is replaced wholesale."""
    stale = (
        "# TokenPak Companion\n\n"
        "## Available MCP tools\n\n"
        "- **estimate_tokens** — old text.\n"
        "- **check_budget** — old text.\n\n"
        "# User Rules\n\nKeep these.\n"
    )
    (tmp_path / "AGENTS.md").write_text(stale)
    content = install_agents_md(target=str(tmp_path)).read_text()
    assert "old text" not in content
    assert "vault_search" in content
    assert "Keep these." in content


# ---------------------------------------------------------------------------
# doctor: AGENTS.override.md shadowing
# ---------------------------------------------------------------------------

def test_override_check_passes_when_absent(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    status, detail = doctor.check_agents_override()
    assert status == "PASS"
    assert "no AGENTS.override.md" in detail


def test_override_check_warns_when_shadowing(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "AGENTS.override.md").write_text("# My Rules\n\nDo my thing.\n")
    status, detail = doctor.check_agents_override()
    assert status == "WARN"
    assert "shadow" in detail.lower()
    assert "AGENTS.override.md" in detail


def test_override_check_passes_when_override_carries_tokenpak_section(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "AGENTS.override.md").write_text(
        "# My Rules\n\n# TokenPak Companion\n\ntools here\n"
    )
    status, detail = doctor.check_agents_override()
    assert status == "PASS"
    assert "TokenPak section" in detail


def test_override_check_is_registered_in_doctor_checks():
    names = [name for name, _ in doctor.CHECKS]
    assert "AGENTS.override shadowing" in names
