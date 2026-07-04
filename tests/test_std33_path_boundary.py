# SPDX-License-Identifier: Apache-2.0
"""Std 33 path-boundary proof for the credentials store (D3 P-A).

The credentials writer (``tokenpak.creds.store``) and the runtime reader
(``tokenpak.creds.providers.user_config``) must resolve the *same*
``credentials.toml`` under the Std 33 TokenPak home so a fresh install
never writes to one path and reads from another.

These tests use only synthetic, obviously-fake secret material — they
never read, print, move, or rotate real credentials.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tokenpak import _paths
from tokenpak.creds import store
from tokenpak.creds.providers import user_config

# Obviously-fake secret material — never a real key.
_FAKE_KEY = "sk-FAKE-not-a-real-key-000"
_FAKE_ID = "openai-test"


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Isolate HOME and clear the TOKENPAK_HOME override for each test."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv(_paths.ENV_VAR, raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# Writer and reader resolve the SAME file under every resolution regime
# ---------------------------------------------------------------------------


def test_std33_writer_reader_same_path_fresh(fake_home):
    """Fresh install (canonical ~/.tpk/): writer == reader == resolver."""
    expected = _paths.under("credentials.toml")
    assert store._config_path() == expected
    assert user_config._config_path() == expected
    assert expected == fake_home / _paths.CANONICAL_DIRNAME / "credentials.toml"


def test_std33_writer_reader_same_path_legacy(fake_home):
    """Legacy fallback (~/.tokenpak/ present, ~/.tpk/ absent): both agree."""
    (fake_home / _paths.LEGACY_DIRNAME).mkdir()
    expected = fake_home / _paths.LEGACY_DIRNAME / "credentials.toml"
    assert store._config_path() == expected
    assert user_config._config_path() == expected


def test_std33_writer_reader_same_path_env_override(fake_home, monkeypatch):
    """TOKENPAK_HOME override: writer and reader both honour it."""
    override = fake_home / "sandbox-home"
    monkeypatch.setenv(_paths.ENV_VAR, str(override))
    expected = override / "credentials.toml"
    assert store._config_path() == expected
    assert user_config._config_path() == expected


# ---------------------------------------------------------------------------
# End-to-end: a write is discoverable + resolvable by the reader (no split)
# ---------------------------------------------------------------------------


def test_std33_creds_roundtrip_fresh_install(fake_home):
    """Fresh install: store.add writes canonical; user_config reads it back."""
    store.add(_FAKE_ID, {"platform": "openai", "kind": "api_key", "key": _FAKE_KEY})

    written = fake_home / _paths.CANONICAL_DIRNAME / "credentials.toml"
    assert written.exists(), "writer must land under canonical ~/.tpk/"
    # It must NOT have created a divergent legacy file.
    assert not (fake_home / _paths.LEGACY_DIRNAME / "credentials.toml").exists()

    discovered = {c.id: c for c in user_config.discover()}
    assert _FAKE_ID in discovered
    cred = discovered[_FAKE_ID]
    assert cred.platform == "openai"
    # The reader resolves the secret from the same file the writer wrote.
    assert user_config.resolve(cred) == _FAKE_KEY
    # The recorded source points at the resolved canonical path.
    assert cred.source.startswith(str(written))


def test_std33_creds_roundtrip_legacy_home(fake_home):
    """Existing install on ~/.tokenpak/: writer + reader stay on legacy."""
    (fake_home / _paths.LEGACY_DIRNAME).mkdir()

    store.add(_FAKE_ID, {"platform": "anthropic", "kind": "api_key", "key": _FAKE_KEY})

    legacy_file = fake_home / _paths.LEGACY_DIRNAME / "credentials.toml"
    assert legacy_file.exists(), "existing legacy install must keep using ~/.tokenpak/"
    assert not (fake_home / _paths.CANONICAL_DIRNAME / "credentials.toml").exists()

    creds = {c.id: c for c in user_config.discover()}
    assert _FAKE_ID in creds
    assert user_config.resolve(creds[_FAKE_ID]) == _FAKE_KEY


def test_std33_creds_roundtrip_tokenpak_home_override(fake_home, monkeypatch):
    """TOKENPAK_HOME sandbox: round-trip lands entirely under the override."""
    override = fake_home / "sandbox-home"
    monkeypatch.setenv(_paths.ENV_VAR, str(override))

    store.add(_FAKE_ID, {"platform": "openai", "kind": "api_key", "key": _FAKE_KEY})

    assert (override / "credentials.toml").exists()
    creds = {c.id: c for c in user_config.discover()}
    assert user_config.resolve(creds[_FAKE_ID]) == _FAKE_KEY


# ---------------------------------------------------------------------------
# Owner-only 0600 file permissions are preserved (requirement #3)
# ---------------------------------------------------------------------------


def test_std33_creds_file_is_0600(fake_home):
    written = store.save({_FAKE_ID: {"platform": "openai", "kind": "api_key", "key": _FAKE_KEY}})
    assert (written.stat().st_mode & 0o777) == 0o600
    assert user_config.config_perms_ok() is True


def test_std33_config_perms_ok_flags_loose_mode(fake_home):
    written = store.save({_FAKE_ID: {"platform": "openai", "kind": "api_key", "key": _FAKE_KEY}})
    os.chmod(written, 0o644)
    assert user_config.config_perms_ok() is False
