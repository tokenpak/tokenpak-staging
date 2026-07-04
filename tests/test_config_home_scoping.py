# SPDX-License-Identifier: Apache-2.0
"""Regression tests — config-file scoped-home resolution.

Locks the ratified config-home-scoping contract (child packet
``p2-config-file-scoping-2026-07-03``): the two config-file defaults resolve
through :func:`tokenpak._paths.under`, so they honor ``TOKENPAK_HOME`` and the
shared ``~/.tpk`` / ``~/.tokenpak`` resolution instead of hardcoding
``~/.tokenpak``.

Contract:
  * ``core/config_loader.CONFIG_PATH`` default -> ``_paths.under("config.yaml")``;
    ``TOKENPAK_CONFIG`` override still wins.
  * ``proxy/config._load_tokenpak_upstream_overrides`` reads
    ``_paths.under("config.json")``.
  * Default (unset ``TOKENPAK_HOME``) is byte-identical for existing legacy
    installs (``~/.tokenpak`` present, ``~/.tpk`` absent) via the resolver
    legacy fallback.

``CONFIG_PATH`` is a module-level constant evaluated at import from the
environment, so the config_loader tests reload the module under a patched
environment inside a ``monkeypatch.context()`` (env restored on block exit) and
an autouse fixture reloads it afterward so no patched value leaks across tests.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from tokenpak import _paths
from tokenpak.core import config_loader
from tokenpak.proxy import config as proxy_config


@pytest.fixture(autouse=True)
def _restore_config_loader():
    """Reload config_loader after each test to restore its real-env CONFIG_PATH."""
    yield
    importlib.reload(config_loader)


def _reload_config_loader():
    return importlib.reload(config_loader)


# ---------------------------------------------------------------------------
# core/config_loader.CONFIG_PATH — config.yaml default
# ---------------------------------------------------------------------------


def test_scoped_home_routes_config_yaml_under_tokenpak_home(tmp_path, monkeypatch):
    """TOKENPAK_HOME set -> config.yaml default resolves under the scoped home."""
    with monkeypatch.context() as m:
        m.setenv("TOKENPAK_HOME", str(tmp_path))
        m.delenv("TOKENPAK_CONFIG", raising=False)
        reloaded = _reload_config_loader()
        assert reloaded.CONFIG_PATH == tmp_path / "config.yaml"
        assert reloaded.CONFIG_PATH == _paths.under("config.yaml")


def test_tokenpak_config_override_still_wins_over_scoped_default(tmp_path, monkeypatch):
    """TOKENPAK_CONFIG wins over the scoped-home default (contract preserved)."""
    custom = tmp_path / "custom" / "myconfig.yaml"
    with monkeypatch.context() as m:
        m.setenv("TOKENPAK_HOME", str(tmp_path / "home"))
        m.setenv("TOKENPAK_CONFIG", str(custom))
        reloaded = _reload_config_loader()
        assert reloaded.CONFIG_PATH == custom


def test_default_unset_home_legacy_present_is_byte_identical(tmp_path, monkeypatch):
    """Unset TOKENPAK_HOME + legacy ~/.tokenpak present (no ~/.tpk):
    resolves to the legacy config.yaml -> byte-identical to the pre-change default."""
    fake_home = tmp_path / "user"
    (fake_home / ".tokenpak").mkdir(parents=True)
    with monkeypatch.context() as m:
        m.delenv("TOKENPAK_HOME", raising=False)
        m.delenv("TOKENPAK_CONFIG", raising=False)
        m.setattr(Path, "home", lambda: fake_home)
        reloaded = _reload_config_loader()
        assert reloaded.CONFIG_PATH == fake_home / ".tokenpak" / "config.yaml"
        assert reloaded.CONFIG_PATH == _paths.under("config.yaml")


def test_default_unset_home_migrated_defers_to_paths(tmp_path, monkeypatch):
    """Unset TOKENPAK_HOME + canonical ~/.tpk present: config now defers to
    _paths (split-brain fix) and reads the canonical home, like all other state."""
    fake_home = tmp_path / "user"
    (fake_home / ".tpk").mkdir(parents=True)
    with monkeypatch.context() as m:
        m.delenv("TOKENPAK_HOME", raising=False)
        m.delenv("TOKENPAK_CONFIG", raising=False)
        m.setattr(Path, "home", lambda: fake_home)
        reloaded = _reload_config_loader()
        assert reloaded.CONFIG_PATH == fake_home / ".tpk" / "config.yaml"
        assert reloaded.CONFIG_PATH == _paths.under("config.yaml")


# ---------------------------------------------------------------------------
# proxy/config._load_tokenpak_upstream_overrides — config.json read
# ---------------------------------------------------------------------------


def _provider_cfg(base_url: str) -> str:
    return json.dumps(
        {
            "models": {
                "providers": {
                    "anthropic": {"base_url": base_url},
                    "tokenpak-anthropic": {"source_provider": "anthropic"},
                }
            }
        }
    )


def test_upstream_overrides_reads_scoped_config_json(tmp_path, monkeypatch):
    """config.json read is scoped to TOKENPAK_HOME, not the legacy ~/.tokenpak
    that the old hardcoded path (Path.home()/.tokenpak/config.json) would have hit."""
    scoped = tmp_path / "scoped"
    scoped.mkdir()
    (scoped / "config.json").write_text(_provider_cfg("https://scoped.example/v1"))

    fake_home = tmp_path / "user"
    (fake_home / ".tokenpak").mkdir(parents=True)
    (fake_home / ".tokenpak" / "config.json").write_text(
        _provider_cfg("https://legacy.example/v1")
    )

    with monkeypatch.context() as m:
        m.setenv("TOKENPAK_HOME", str(scoped))
        m.setattr(Path, "home", lambda: fake_home)
        overrides = proxy_config._load_tokenpak_upstream_overrides()

    assert overrides.get("anthropic-messages") == "https://scoped.example/v1"
    assert "legacy.example" not in json.dumps(overrides)


def test_upstream_overrides_no_leak_to_legacy_when_scoped_absent(tmp_path, monkeypatch):
    """No scoped config.json -> falls back to the config.yaml loader (stubbed empty);
    a planted legacy ~/.tokenpak/config.json must NOT leak through."""
    scoped = tmp_path / "scoped"
    scoped.mkdir()  # deliberately no config.json here

    fake_home = tmp_path / "user"
    (fake_home / ".tokenpak").mkdir(parents=True)
    (fake_home / ".tokenpak" / "config.json").write_text(
        _provider_cfg("https://legacy.example/v1")
    )

    with monkeypatch.context() as m:
        m.setenv("TOKENPAK_HOME", str(scoped))
        m.setattr(Path, "home", lambda: fake_home)
        m.setattr(config_loader, "load_config", lambda *a, **k: {})
        overrides = proxy_config._load_tokenpak_upstream_overrides()

    assert overrides == {}
