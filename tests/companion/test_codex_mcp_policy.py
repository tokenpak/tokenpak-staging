# SPDX-License-Identifier: Apache-2.0
"""MCP policy block for the companion server in Codex ``config.toml``.

The companion must register with an explicit policy, not Codex defaults:

- ``startup_timeout_sec = 30`` (Python cold-start headroom)
- ``tool_timeout_sec = 60``
- ``enabled_tools`` allowlist generated from the MCP TOOLS registry
- ``default_tools_approval_mode = "auto"`` for read-shaped tools
- ``tool_approvals`` prompting for mutating tools
  (``journal_write``, ``prune_context``)

Covers rendering, application (TOML surgery that preserves foreign keys),
verification, the doctor check, and the register() wiring.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from tokenpak.companion.codex import doctor, mcp_config
from tokenpak.companion.mcp.tools import TOOLS

_REGISTERED_CONFIG = textwrap.dedent(
    """\
    model = "gpt-5-codex"

    [mcp_servers.tokenpak-companion]
    command = "python3"
    args = ["-P", "-m", "tokenpak.companion.mcp.server"]

    [mcp_servers.other-server]
    command = "other"
    """
)


def _write_config(home: Path, body: str) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# expected_policy / render_policy_lines
# ---------------------------------------------------------------------------

def test_expected_policy_values():
    policy = mcp_config.expected_policy()
    assert policy["startup_timeout_sec"] == 30
    assert policy["tool_timeout_sec"] == 60
    assert policy["default_tools_approval_mode"] == "auto"


def test_allowlist_tracks_registry():
    policy = mcp_config.expected_policy()
    assert policy["enabled_tools"] == [t.name for t in TOOLS]
    assert "vault_search" in policy["enabled_tools"]
    assert "vault_retrieve" in policy["enabled_tools"]


def test_mutating_tools_require_prompt_approval():
    policy = mcp_config.expected_policy()
    assert policy["tool_approvals"] == {
        "journal_write": "prompt",
        "prune_context": "prompt",
    }


def test_rendered_policy_lines_parse_as_toml_and_match_expected():
    text = "\n".join(mcp_config.render_policy_lines()) + "\n"
    data = mcp_config._loads_toml(text)
    assert data == mcp_config.expected_policy()


# ---------------------------------------------------------------------------
# apply_policy
# ---------------------------------------------------------------------------

def test_apply_policy_inserts_block_preserving_existing_keys(tmp_path: Path):
    path = _write_config(tmp_path, _REGISTERED_CONFIG)
    ok, detail = mcp_config.apply_policy(path)
    assert ok, detail

    data = mcp_config._loads_toml(path.read_text())
    table = data["mcp_servers"][mcp_config.SERVER_NAME]
    # Foreign keys preserved.
    assert table["command"] == "python3"
    assert table["args"] == ["-P", "-m", "tokenpak.companion.mcp.server"]
    # Policy keys present.
    for key, value in mcp_config.expected_policy().items():
        assert table[key] == value
    # Other sections untouched.
    assert data["model"] == "gpt-5-codex"
    assert data["mcp_servers"]["other-server"]["command"] == "other"


def test_apply_policy_is_idempotent(tmp_path: Path):
    path = _write_config(tmp_path, _REGISTERED_CONFIG)
    assert mcp_config.apply_policy(path)[0]
    first = path.read_text()
    assert mcp_config.apply_policy(path)[0]
    assert path.read_text() == first


def test_apply_policy_replaces_stale_values(tmp_path: Path):
    stale = _REGISTERED_CONFIG.replace(
        'command = "python3"',
        'command = "python3"\nstartup_timeout_sec = 5\nenabled_tools = ["estimate_tokens"]',
    )
    path = _write_config(tmp_path, stale)
    ok, detail = mcp_config.apply_policy(path)
    assert ok, detail
    data = mcp_config._loads_toml(path.read_text())
    table = data["mcp_servers"][mcp_config.SERVER_NAME]
    assert table["startup_timeout_sec"] == 30
    assert table["enabled_tools"] == [t.name for t in TOOLS]
    # No duplicate key lines survived (the file still parses, checked above).
    assert path.read_text().count("startup_timeout_sec") == 1


def test_apply_policy_refuses_when_server_not_registered(tmp_path: Path):
    path = _write_config(tmp_path, 'model = "gpt-5-codex"\n')
    before = path.read_text()
    ok, detail = mcp_config.apply_policy(path)
    assert not ok
    assert "register" in detail.lower()
    assert path.read_text() == before


def test_apply_policy_refuses_when_config_missing(tmp_path: Path):
    ok, detail = mcp_config.apply_policy(tmp_path / "config.toml")
    assert not ok
    assert "missing" in detail.lower()


def test_apply_policy_refuses_unparseable_config(tmp_path: Path):
    path = _write_config(tmp_path, "not [valid toml ===\n")
    ok, detail = mcp_config.apply_policy(path)
    assert not ok
    assert "parse" in detail.lower()


# ---------------------------------------------------------------------------
# verify_policy
# ---------------------------------------------------------------------------

def test_verify_policy_passes_after_apply(tmp_path: Path):
    path = _write_config(tmp_path, _REGISTERED_CONFIG)
    mcp_config.apply_policy(path)
    ok, problems = mcp_config.verify_policy(path)
    assert ok, problems
    assert problems == []


def test_verify_policy_accepts_higher_timeouts(tmp_path: Path):
    path = _write_config(tmp_path, _REGISTERED_CONFIG)
    mcp_config.apply_policy(path)
    raised = path.read_text().replace(
        "startup_timeout_sec = 30", "startup_timeout_sec = 45"
    ).replace("tool_timeout_sec = 60", "tool_timeout_sec = 120")
    path.write_text(raised)
    ok, problems = mcp_config.verify_policy(path)
    assert ok, problems


def test_verify_policy_flags_low_startup_timeout(tmp_path: Path):
    path = _write_config(tmp_path, _REGISTERED_CONFIG)
    mcp_config.apply_policy(path)
    path.write_text(
        path.read_text().replace("startup_timeout_sec = 30", "startup_timeout_sec = 5")
    )
    ok, problems = mcp_config.verify_policy(path)
    assert not ok
    assert any("startup_timeout_sec" in p for p in problems)


def test_verify_policy_flags_missing_policy_entirely(tmp_path: Path):
    path = _write_config(tmp_path, _REGISTERED_CONFIG)
    ok, problems = mcp_config.verify_policy(path)
    assert not ok
    joined = " ".join(problems)
    assert "startup_timeout_sec" in joined
    assert "enabled_tools" in joined
    assert "tool_approvals" in joined


def test_verify_policy_flags_allowlist_drift(tmp_path: Path):
    path = _write_config(tmp_path, _REGISTERED_CONFIG)
    mcp_config.apply_policy(path)
    drifted = path.read_text().replace('"vault_retrieve"', '"bogus_tool"')
    path.write_text(drifted)
    ok, problems = mcp_config.verify_policy(path)
    assert not ok
    joined = " ".join(problems)
    assert "vault_retrieve" in joined
    assert "bogus_tool" in joined


def test_verify_policy_flags_mutating_tool_without_prompt(tmp_path: Path):
    path = _write_config(tmp_path, _REGISTERED_CONFIG)
    mcp_config.apply_policy(path)
    relaxed = path.read_text().replace('journal_write = "prompt"', 'journal_write = "auto"')
    path.write_text(relaxed)
    ok, problems = mcp_config.verify_policy(path)
    assert not ok
    assert any("journal_write" in p for p in problems)


def test_verify_policy_flags_wrong_default_mode(tmp_path: Path):
    path = _write_config(tmp_path, _REGISTERED_CONFIG)
    mcp_config.apply_policy(path)
    path.write_text(
        path.read_text().replace(
            'default_tools_approval_mode = "auto"',
            'default_tools_approval_mode = "never"',
        )
    )
    ok, problems = mcp_config.verify_policy(path)
    assert not ok
    assert any("default_tools_approval_mode" in p for p in problems)


def test_codex_config_path_honors_codex_home(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert mcp_config.codex_config_path() == tmp_path / "config.toml"


# ---------------------------------------------------------------------------
# doctor: check_mcp_policy
# ---------------------------------------------------------------------------

def test_doctor_policy_skips_when_config_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    status, detail = doctor.check_mcp_policy()
    assert status == "PASS"
    assert "skipped" in detail.lower()


def test_doctor_policy_skips_when_server_not_in_config(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_config(tmp_path, 'model = "gpt-5-codex"\n')
    status, detail = doctor.check_mcp_policy()
    assert status == "PASS"
    assert "skipped" in detail.lower()


def test_doctor_policy_fails_when_registered_without_policy(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_config(tmp_path, _REGISTERED_CONFIG)
    status, detail = doctor.check_mcp_policy()
    assert status == "FAIL"
    assert "tokenpak codex install" in detail


def test_doctor_policy_passes_when_policy_applied(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = _write_config(tmp_path, _REGISTERED_CONFIG)
    mcp_config.apply_policy(path)
    status, detail = doctor.check_mcp_policy()
    assert status == "PASS"
    assert "allowlist" in detail
    assert "approval" in detail


def test_doctor_policy_check_is_registered_in_checks():
    names = [name for name, _ in doctor.CHECKS]
    assert "MCP config policy" in names


# ---------------------------------------------------------------------------
# register() wires policy application
# ---------------------------------------------------------------------------

class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_register_applies_policy_after_add(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"

    monkeypatch.setattr(mcp_config, "is_registered", lambda: False)

    def fake_run(cmd, *args, **kwargs):
        assert cmd[:3] == ["codex", "mcp", "add"]
        # Simulate codex writing the server table on `mcp add`.
        _write_config(tmp_path, _REGISTERED_CONFIG)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(mcp_config.subprocess, "run", fake_run)

    assert mcp_config.register() is True
    ok, problems = mcp_config.verify_policy(config_path)
    assert ok, problems


def test_register_applies_policy_when_already_registered(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config_path = _write_config(tmp_path, _REGISTERED_CONFIG)
    monkeypatch.setattr(mcp_config, "is_registered", lambda: True)

    assert mcp_config.register() is True
    ok, problems = mcp_config.verify_policy(config_path)
    assert ok, problems


def test_register_failure_does_not_write_policy(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(mcp_config, "is_registered", lambda: False)

    def fake_run(cmd, *args, **kwargs):
        return _FakeCompleted(returncode=1, stderr="boom")

    monkeypatch.setattr(mcp_config.subprocess, "run", fake_run)

    assert mcp_config.register() is False
    assert not (tmp_path / "config.toml").exists()
