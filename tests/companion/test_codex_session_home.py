# SPDX-License-Identifier: Apache-2.0
"""Tests for isolated CODEX_HOME provisioning (Option C).

Covers mode routing, workspace/isolated home shapes, symlink-based auth/
config propagation (never copy — no refresh-token fork), the PID sentinel,
orphan detection, retention sweeping, and ``clean``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tokenpak.companion.codex import session_home as sh


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    """Point HOME at a tmp dir with a populated canonical ~/.codex."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv(sh.ENV_SESSION_MODE, raising=False)
    codex = tmp_path / ".codex"
    codex.mkdir(parents=True)
    (codex / "auth.json").write_text('{"tokens": {"access_token": "tok"}}')
    (codex / "config.toml").write_text('model = "gpt-5-codex"\n')
    # state DB must NOT be propagated.
    (codex / "state_5.sqlite").write_text("STATE")
    return tmp_path


# ── mode resolution ──────────────────────────────────────────────────

def test_resolve_mode_default_is_shared(fake_home):
    assert sh.resolve_mode() == sh.MODE_SHARED


@pytest.mark.parametrize("value", ["workspace", "WORKSPACE", " workspace "])
def test_resolve_mode_workspace(fake_home, monkeypatch, value):
    monkeypatch.setenv(sh.ENV_SESSION_MODE, value)
    assert sh.resolve_mode() == sh.MODE_WORKSPACE


def test_resolve_mode_isolated(fake_home, monkeypatch):
    monkeypatch.setenv(sh.ENV_SESSION_MODE, "isolated")
    assert sh.resolve_mode() == sh.MODE_ISOLATED


def test_resolve_mode_unknown_falls_back_to_shared(fake_home, monkeypatch):
    monkeypatch.setenv(sh.ENV_SESSION_MODE, "garbage-value")
    assert sh.resolve_mode() == sh.MODE_SHARED


def test_resolve_mode_attach_is_recognized_but_deferred(fake_home, monkeypatch):
    monkeypatch.setenv(sh.ENV_SESSION_MODE, "attach")
    assert sh.resolve_mode() == sh.MODE_ATTACH


# ── shared mode is a no-op (existing behavior unchanged) ──────────────

def test_provision_shared_returns_canonical_home_uncreated(fake_home):
    res = sh.provision_codex_home("shared")
    assert res.mode == sh.MODE_SHARED
    assert res.home == fake_home / ".codex"
    assert res.created is False
    assert res.propagated == []


def test_provision_shared_respects_existing_codex_home_env(fake_home, monkeypatch):
    custom = fake_home / "custom-codex"
    monkeypatch.setenv("CODEX_HOME", str(custom))
    res = sh.provision_codex_home("shared")
    assert res.home == custom


def test_attach_mode_raises_not_implemented(fake_home):
    with pytest.raises(NotImplementedError):
        sh.provision_codex_home("attach")


# ── workspace mode ────────────────────────────────────────────────────

def test_provision_workspace_is_per_project_and_stable(fake_home):
    proj = fake_home / "projects" / "alpha"
    proj.mkdir(parents=True)
    first = sh.provision_codex_home("workspace", workspace_dir=proj)
    second = sh.provision_codex_home("workspace", workspace_dir=proj)
    assert first.home == second.home
    assert first.home.parent == sh.workspaces_root()
    assert first.created is True
    assert second.created is False


def test_provision_workspace_distinct_projects_distinct_homes(fake_home):
    a = fake_home / "projects" / "a"
    b = fake_home / "projects" / "b"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    ha = sh.provision_codex_home("workspace", workspace_dir=a)
    hb = sh.provision_codex_home("workspace", workspace_dir=b)
    assert ha.home != hb.home


def test_workspace_hash_collapses_equivalent_paths(fake_home):
    proj = fake_home / "projects" / "alpha"
    proj.mkdir(parents=True)
    direct = sh.workspace_hash(proj)
    via_dotdot = sh.workspace_hash(proj / ".." / "alpha")
    assert direct == via_dotdot


# ── isolated mode ─────────────────────────────────────────────────────

def test_provision_isolated_is_unique_per_call(fake_home):
    one = sh.provision_codex_home("isolated")
    two = sh.provision_codex_home("isolated")
    assert one.home != two.home
    assert one.home.parent == sh.sessions_root()


def test_provision_isolated_honors_explicit_session_id(fake_home):
    res = sh.provision_codex_home("isolated", session_id="sess-xyz")
    assert res.home.name == "sess-xyz"


# ── auth/config propagation = symlink, never copy ─────────────────────

def test_auth_and_config_are_symlinked_not_copied(fake_home):
    res = sh.provision_codex_home("isolated")
    auth = res.home / "auth.json"
    config = res.home / "config.toml"
    assert auth.is_symlink()
    assert config.is_symlink()
    assert os.readlink(auth) == str(fake_home / ".codex" / "auth.json")
    assert "auth.json" in res.propagated
    assert "config.toml" in res.propagated


def test_state_db_is_never_propagated(fake_home):
    res = sh.provision_codex_home("isolated")
    assert not (res.home / "state_5.sqlite").exists()


def test_propagation_is_idempotent(fake_home):
    first = sh.provision_codex_home("workspace", workspace_dir=fake_home)
    # re-provision same home; symlink should still be the canonical link
    second = sh.provision_codex_home("workspace", workspace_dir=fake_home)
    assert first.home == second.home
    assert (second.home / "auth.json").is_symlink()
    assert os.readlink(second.home / "auth.json") == str(
        fake_home / ".codex" / "auth.json"
    )


def test_propagation_replaces_stale_real_file_with_symlink(fake_home):
    res = sh.provision_codex_home("isolated", session_id="s1")
    # simulate a stale real copy shadowing the canonical credential
    auth = res.home / "auth.json"
    auth.unlink()
    auth.write_text('{"stale": true}')
    again = sh.provision_codex_home("isolated", session_id="s1")
    assert again.home == res.home
    assert (again.home / "auth.json").is_symlink()


def test_missing_canonical_auth_is_skipped_not_copied(fake_home):
    (fake_home / ".codex" / "auth.json").unlink()
    res = sh.provision_codex_home("isolated")
    assert not (res.home / "auth.json").exists()
    assert "auth.json" not in res.propagated
    assert "config.toml" in res.propagated


# ── env application + PID sentinel ────────────────────────────────────

def test_apply_to_env_sets_codex_home_without_mutating_os_environ(fake_home):
    res = sh.provision_codex_home("isolated")
    env = sh.apply_to_env(res.home, {"PATH": "/bin"})
    assert env["CODEX_HOME"] == str(res.home)
    assert env["PATH"] == "/bin"
    assert "CODEX_HOME" not in os.environ


def test_record_pid_writes_sentinel(fake_home):
    res = sh.provision_codex_home("isolated")
    sh.record_pid(res.home, pid=os.getpid())
    assert (res.home / "codex.pid").read_text().strip() == str(os.getpid())


# ── orphan detection ──────────────────────────────────────────────────

def test_is_orphaned_true_for_dead_pid(fake_home):
    res = sh.provision_codex_home("isolated")
    sh.record_pid(res.home, pid=2147480000)  # implausibly high → dead
    assert sh.is_orphaned(res.home) is True


def test_is_orphaned_false_for_live_pid(fake_home):
    res = sh.provision_codex_home("isolated")
    sh.record_pid(res.home, pid=os.getpid())
    assert sh.is_orphaned(res.home) is False


def test_is_orphaned_true_when_no_pid_recorded(fake_home):
    res = sh.provision_codex_home("isolated")
    assert sh.is_orphaned(res.home) is True


# ── list_homes ────────────────────────────────────────────────────────

def test_list_homes_reports_mode_and_liveness(fake_home):
    iso = sh.provision_codex_home("isolated")
    sh.record_pid(iso.home, pid=os.getpid())
    ws = sh.provision_codex_home("workspace", workspace_dir=fake_home)
    homes = {h.path: h for h in sh.list_homes()}
    assert homes[iso.home].mode == sh.MODE_ISOLATED
    assert homes[iso.home].alive is True
    assert homes[ws.home].mode == sh.MODE_WORKSPACE


# ── retention sweep (count) ───────────────────────────────────────────

def test_retention_sweep_caps_isolated_home_count(fake_home):
    # Provision more than the cap; all orphaned (no live PID recorded).
    for i in range(sh.RETENTION_MAX_HOMES + 4):
        sh.provision_codex_home("isolated", session_id=f"sess-{i:02d}")
    remaining = [h for h in sh.list_homes(include_workspaces=False)]
    # sweep runs at provision time; after the last provision the count is
    # bounded to <= cap (a slot is reserved for the new home).
    assert len(remaining) <= sh.RETENTION_MAX_HOMES


def test_retention_sweep_never_removes_live_home(fake_home, monkeypatch):
    live = sh.provision_codex_home("isolated", session_id="live")
    sh.record_pid(live.home, pid=os.getpid())
    for i in range(sh.RETENTION_MAX_HOMES + 4):
        sh.provision_codex_home("isolated", session_id=f"dead-{i:02d}")
    assert live.home.exists()


def test_retention_sweep_never_touches_workspace_homes(fake_home):
    ws = sh.provision_codex_home("workspace", workspace_dir=fake_home)
    for i in range(sh.RETENTION_MAX_HOMES + 4):
        sh.provision_codex_home("isolated", session_id=f"d-{i:02d}")
    assert ws.home.exists()


# ── clean ─────────────────────────────────────────────────────────────

def test_clean_removes_orphaned_isolated_homes_only_by_default(fake_home):
    orphan = sh.provision_codex_home("isolated", session_id="orphan")
    ws = sh.provision_codex_home("workspace", workspace_dir=fake_home)
    removed = sh.clean()
    assert orphan.home in removed
    assert ws.home not in removed
    assert ws.home.exists()


def test_clean_workspace_flag_includes_orphaned_workspaces(fake_home):
    ws = sh.provision_codex_home("workspace", workspace_dir=fake_home)
    removed = sh.clean(include_workspaces=True)
    assert ws.home in removed


def test_clean_skips_live_homes_without_force(fake_home):
    live = sh.provision_codex_home("isolated", session_id="live")
    sh.record_pid(live.home, pid=os.getpid())
    removed = sh.clean()
    assert live.home not in removed
    assert live.home.exists()


def test_clean_removing_home_does_not_delete_canonical_creds(fake_home):
    res = sh.provision_codex_home("isolated", session_id="x")
    sh.clean()  # orphan → removed
    # canonical auth.json (symlink target) must survive rmtree of the home
    assert (fake_home / ".codex" / "auth.json").exists()
    assert (fake_home / ".codex" / "config.toml").exists()
