# SPDX-License-Identifier: Apache-2.0
"""Determinism guards for TokenPak-controlled Codex prompt artifacts.

Codex CLI talks directly to OpenAI (it is not routed through the TokenPak
proxy), so OpenAI's automatic, prefix-based prompt cache applies. That cache
is silently eroded if a TokenPak-controlled artifact that lands in the prompt
prefix (e.g. the installed AGENTS.md, or the MCP tool definitions) drifts
between runs. These tests are purely preventative: they pin the artifacts to
byte-deterministic output so a future edit cannot smuggle volatile content
into the prefix without a test failing.

All tests are read-only / offline: no Codex launch, no network, no proxy, no
prompt/session persistence.
"""

from __future__ import annotations

import re

from tokenpak.companion.codex.agents_md import generate_agents_md
from tokenpak.companion.mcp import server as mcp_server
from tokenpak.companion.mcp.tools import TOOLS

# ---------------------------------------------------------------------------
# 1. generate_agents_md() is byte-deterministic across repeated calls
# ---------------------------------------------------------------------------

def test_generate_agents_md_is_byte_identical_across_calls():
    """Two calls with the same (no) inputs must return identical bytes."""
    first = generate_agents_md()
    second = generate_agents_md()
    assert first == second
    assert first.encode("utf-8") == second.encode("utf-8")


def test_generate_agents_md_is_static_constant_no_percall_computation():
    """Output must be the module constant itself (no per-call assembly)."""
    from tokenpak.companion.codex import agents_md

    assert generate_agents_md() is agents_md._AGENTS_CONTENT


def test_generate_agents_md_stable_over_many_calls():
    outputs = {generate_agents_md() for _ in range(50)}
    assert len(outputs) == 1


# ---------------------------------------------------------------------------
# 2. AGENTS.md content carries no dynamic / volatile tokens
# ---------------------------------------------------------------------------

# Deny-list of patterns that would indicate volatile content leaked into the
# prefix artifact. Each entry is (label, compiled-regex).
_DYNAMIC_PATTERNS = [
    ("iso-date", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    ("clock-time", re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b")),
    ("timestamp-word", re.compile(r"(?i)\btimestamp\b")),
    ("epoch-seconds", re.compile(r"\b1[0-9]{9}\b")),  # 10-digit unix epoch
    ("uuid", re.compile(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
        r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    )),
    ("session-id", re.compile(r"(?i)\bsession[_-]?id\s*[:=]")),
    ("trace-id", re.compile(r"(?i)\btrace[_-]?id\s*[:=]")),
    ("env-interpolation", re.compile(r"\$\{?[A-Z_][A-Z0-9_]*\}?")),
    ("cwd-token", re.compile(r"(?i)\bcwd\b\s*[:=]")),
    ("dollar-rate", re.compile(r"\$\s?\d+(\.\d+)?\s*(/|per)\s*\b")),
]


def test_agents_md_has_no_dynamic_tokens():
    content = generate_agents_md()
    hits = []
    for label, pat in _DYNAMIC_PATTERNS:
        m = pat.search(content)
        if m:
            hits.append(f"{label}: {m.group(0)!r}")
    assert not hits, f"AGENTS.md contains volatile token(s): {hits}"


def test_agents_md_utf8_roundtrip_is_stable():
    """Content is fixed text (deterministic bytes), with no control chars that
    could vary by environment. Non-ASCII typography (e.g. em-dashes) is fine —
    what matters for prefix-cache stability is that the bytes never change."""
    content = generate_agents_md()
    assert content == content.encode("utf-8").decode("utf-8")
    # No control characters except the newline used for formatting.
    assert not any(ord(ch) < 0x20 and ch != "\n" for ch in content)


# ---------------------------------------------------------------------------
# 3. MCP / TIP tool-definition ordering is deterministic
# ---------------------------------------------------------------------------

def _emit_tool_list():
    """Capture the tool list the MCP server emits on tools/list."""
    captured: list[dict] = []

    def _fake_send(payload):
        captured.append(payload)

    orig_send = mcp_server._send
    mcp_server._send = _fake_send  # type: ignore[assignment]
    try:
        mcp_server._handle_tools_list(1)
    finally:
        mcp_server._send = orig_send  # type: ignore[assignment]

    assert captured, "tools/list emitted no payload"
    return [t["name"] for t in captured[0]["result"]["tools"]]


def test_tool_list_ordering_is_stable_across_calls():
    first = _emit_tool_list()
    second = _emit_tool_list()
    assert first == second


def test_tool_list_matches_registry_order():
    """Emitted order must track the explicit TOOLS registry order exactly."""
    emitted = _emit_tool_list()
    assert emitted == [t.name for t in TOOLS]


def test_tool_names_have_a_single_deterministic_order():
    """Sanity: names are unique (a stable order requires no duplicates)."""
    names = [t.name for t in TOOLS]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# 4. rates_snapshot stays hook-side telemetry only (never in the prompt)
# ---------------------------------------------------------------------------

def test_rates_snapshot_is_not_referenced_by_agents_md():
    """The rate snapshot must not leak into the prompt-facing artifact."""
    content = generate_agents_md().lower()
    for needle in ("model_rates", "rates.tsv", "rates_snapshot", "rates snapshot"):
        assert needle not in content, f"AGENTS.md references rates artifact: {needle!r}"


def test_rates_snapshot_writes_only_its_tsv_target(tmp_path):
    """refresh() must touch only its given path (no prompt artifact write)."""
    from tokenpak.companion.codex import rates_snapshot

    target = tmp_path / "run" / "model_rates.tsv"
    out = rates_snapshot.refresh(path=target)

    assert out == target
    assert target.exists()
    # Only the snapshot file (and its parent dirs) should exist under tmp_path.
    written_files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written_files == [target]


def test_rates_snapshot_target_is_a_run_tsv_not_a_prompt_path():
    """Default snapshot lives under a runtime dir as a .tsv, not a prompt file."""
    from tokenpak.companion.codex import rates_snapshot

    default = rates_snapshot.DEFAULT_SNAPSHOT_PATH
    assert default.suffix == ".tsv"
    assert default.name == "model_rates.tsv"
    # Not an AGENTS.md / prompt artifact path.
    assert "AGENTS" not in str(default)


def test_rates_snapshot_content_is_sorted_deterministic(tmp_path):
    """TSV ordering is sorted -> stable across runs (git/cache friendly)."""
    from tokenpak.companion.codex import rates_snapshot

    target = tmp_path / "model_rates.tsv"
    rates_snapshot.refresh(path=target)
    lines = [ln for ln in target.read_text().splitlines() if ln.strip()]
    assert lines == sorted(lines)
