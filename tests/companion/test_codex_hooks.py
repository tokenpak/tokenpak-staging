# SPDX-License-Identifier: Apache-2.0
"""Regression tests for Codex companion hook feature setup."""
from __future__ import annotations

import subprocess
from pathlib import Path

from tokenpak.companion.codex import doctor
from tokenpak.companion.codex import hooks as codex_hooks


def test_remove_deprecated_hooks_feature_only_removes_features_key(tmp_path: Path) -> None:
    """Regression: stale [features].codex_hooks causes Codex startup warnings."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "model = \"gpt-5.5\"\n"
        "\n"
        "[features]\n"
        "hooks = true\n"
        "codex_hooks = true\n"
        "memories = true\n"
        "\n"
        "[other]\n"
        "codex_hooks = true\n"
    )

    assert codex_hooks.has_deprecated_hooks_feature(config_path) is True
    assert codex_hooks.remove_deprecated_hooks_feature(config_path) is True

    content = config_path.read_text()
    assert "[features]\nhooks = true\nmemories = true" in content
    assert "[other]\ncodex_hooks = true" in content
    assert codex_hooks.has_deprecated_hooks_feature(config_path) is False


def test_remove_deprecated_hooks_feature_is_noop_when_absent(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    original = "[features]\nhooks = true\n"
    config_path.write_text(original)

    assert codex_hooks.remove_deprecated_hooks_feature(config_path) is False
    assert config_path.read_text() == original


def test_ensure_hooks_feature_uses_stable_feature_and_migrates_config(
    tmp_path: Path, monkeypatch
) -> None:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    config_path = codex_dir / "config.toml"
    config_path.write_text("[features]\nhooks = true\ncodex_hooks = true\n")
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(codex_hooks.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(codex_hooks.subprocess, "run", fake_run)

    assert codex_hooks.ensure_hooks_feature_enabled() is True

    assert calls == [["codex", "features", "enable", "hooks"]]
    assert "codex_hooks" not in config_path.read_text()


def test_doctor_checks_stable_hooks_feature(monkeypatch) -> None:
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="hooks     stable  true\ncodex_hooks     deprecated  false\n",
            stderr="",
        )

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)

    assert doctor.check_hooks_feature() == (True, "hooks=true")
