# SPDX-License-Identifier: Apache-2.0
"""Tests for the additive native-status-module installer (Codex telemetry v0).

Contract under test is the *install mechanics* — additive, no-overwrite,
preservation, fallback — none of which require a live Codex.
"""

from __future__ import annotations

import re

import pytest

from tokenpak.companion.codex import statusline_config as sc

try:
    import tomllib as _toml
except ModuleNotFoundError:  # 3.10
    try:
        import tomli as _toml  # type: ignore
    except ModuleNotFoundError:
        _toml = None


def _parse(path):
    assert _toml is not None, "need a TOML reader to validate output"
    return _toml.loads(path.read_text())


# --- additive install (fresh) ------------------------------------------------

def test_install_fresh_adds_all_managed_keys(tmp_path):
    cfg = tmp_path / "config.toml"
    res = sc.install_status_line(cfg)
    assert res["changed"] is True
    assert set(res["added"]) == set(sc._MANAGED_KEYS)
    assert res["skipped"] == []
    data = _parse(cfg)
    assert data["tui"]["status_line"] == sc.DEFAULT_STATUS_ITEMS
    assert data["tui"]["terminal_title"] == sc.DEFAULT_TITLE_ITEMS
    assert data["tui"]["status_line_use_colors"] is True


def test_no_colors_flag(tmp_path):
    cfg = tmp_path / "config.toml"
    sc.install_status_line(cfg, use_colors=False)
    assert _parse(cfg)["tui"]["status_line_use_colors"] is False


# --- no overwrite of user config --------------------------------------------

def test_does_not_overwrite_user_status_line(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[tui]\nstatus_line = ["cwd"]\n')
    res = sc.install_status_line(cfg)
    assert "status_line" in res["skipped"]
    assert "status_line" not in res["added"]
    data = _parse(cfg)
    assert data["tui"]["status_line"] == ["cwd"]  # untouched
    # the other two missing keys still get added
    assert data["tui"]["terminal_title"] == sc.DEFAULT_TITLE_ITEMS


def test_detects_dotted_key_form_and_appends_safely(tmp_path):
    # Dotted form with NO [tui] header: appending a [tui] block would be a TOML
    # "redefine namespace" error — the installer must append dotted keys instead.
    cfg = tmp_path / "config.toml"
    cfg.write_text('tui.status_line = ["usage"]\n')
    res = sc.install_status_line(cfg)
    assert "status_line" in res["skipped"]          # user's dotted key untouched
    assert "terminal_title" in res["added"]
    data = _parse(cfg)                               # must still be valid TOML
    assert data["tui"]["status_line"] == ["usage"]
    assert data["tui"]["terminal_title"] == sc.DEFAULT_TITLE_ITEMS
    assert "[tui]" not in cfg.read_text()            # stayed in dotted form


# --- idempotency -------------------------------------------------------------

def test_idempotent_second_run_is_noop(tmp_path):
    cfg = tmp_path / "config.toml"
    sc.install_status_line(cfg)
    res2 = sc.install_status_line(cfg)
    assert res2["changed"] is False
    assert res2["added"] == []
    # no duplicate [tui] header
    assert cfg.read_text().count("[tui]") == 1


# --- existing config preservation -------------------------------------------

def test_preserves_other_sections_and_comments(tmp_path):
    cfg = tmp_path / "config.toml"
    original = (
        '# my hand-written config\n'
        'model = "gpt-5.5"\n\n'
        '[mcp_servers.foo]\n'
        'command = "/usr/bin/python3"  # keep me\n'
    )
    cfg.write_text(original)
    sc.install_status_line(cfg)
    text = cfg.read_text()
    # every original line survives verbatim
    for line in original.splitlines():
        assert line in text
    data = _parse(cfg)
    assert data["model"] == "gpt-5.5"
    assert data["mcp_servers"]["foo"]["command"] == "/usr/bin/python3"
    assert data["tui"]["status_line"] == sc.DEFAULT_STATUS_ITEMS


def test_inserts_after_existing_tui_header(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[tui]\nkeymap = "vim"\n')
    sc.install_status_line(cfg)
    data = _parse(cfg)
    assert data["tui"]["keymap"] == "vim"  # preserved
    assert data["tui"]["status_line"] == sc.DEFAULT_STATUS_ITEMS
    assert cfg.read_text().count("[tui]") == 1  # no duplicate section


# --- backup ------------------------------------------------------------------

def test_backup_written_when_editing_existing(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('model = "gpt-5.5"\n')
    res = sc.install_status_line(cfg)
    assert res["backup"] is not None
    bak = tmp_path / "config.toml.bak"
    assert bak.exists()
    assert bak.read_text() == 'model = "gpt-5.5"\n'  # pre-edit content


def test_no_backup_for_brand_new_file(tmp_path):
    cfg = tmp_path / "config.toml"
    res = sc.install_status_line(cfg)
    assert res["backup"] is None
    assert not (tmp_path / "config.toml.bak").exists()


# --- fallback / refuse to touch unparseable ----------------------------------

@pytest.mark.skipif(_toml is None, reason="needs TOML reader for parse-failure path")
def test_unparseable_config_is_left_untouched(tmp_path):
    cfg = tmp_path / "config.toml"
    broken = 'this is = = not valid toml ][\n'
    cfg.write_text(broken)
    res = sc.install_status_line(cfg)
    assert res["changed"] is False
    assert cfg.read_text() == broken  # never edited a file we can't parse


# --- no custom / freeform claim ---------------------------------------------

def test_items_are_builtin_ids_only_no_freeform(tmp_path):
    # Every default item must be a bare built-in identifier: no spaces, no 📦,
    # no shell metacharacters, no command/template syntax. This is what keeps
    # the feature "native modules", not a custom PakLine.
    id_re = re.compile(r"^[a-z][a-z0-9_-]*$")
    for item in sc.DEFAULT_STATUS_ITEMS + sc.DEFAULT_TITLE_ITEMS:
        assert id_re.match(item), f"non-native/freeform item: {item!r}"
        assert "📦" not in item
        assert not any(c in item for c in ' "\'`${}|&;')


def test_terminal_title_has_no_semantic_text(tmp_path):
    cfg = tmp_path / "config.toml"
    sc.install_status_line(cfg)
    title = _parse(cfg)["tui"]["terminal_title"]
    assert isinstance(title, list)
    assert all(isinstance(t, str) and " " not in t for t in title)
    assert "📦" not in cfg.read_text()  # no branded/semantic title injected


# --- CODEX_HOME honored ------------------------------------------------------

def test_codex_home_env_honored(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ch"))
    assert sc.default_config_path() == tmp_path / "ch" / "config.toml"
