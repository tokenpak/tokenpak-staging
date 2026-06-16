# SPDX-License-Identifier: Apache-2.0
"""§4 value-plane invariant: `check_proxy_routing` is WARN-not-FAIL.

`tokenpak codex` is observability-first by default — model traffic does not
pass through the TokenPak proxy unless the user explicitly configures a
TokenPak `model_provider`. The doctor must:

- WARN (never FAIL) when Codex is not proxy-routed.
- PASS only when a TokenPak provider is selected AND defined.
- State the value plane truthfully: provider-native cache vs TokenPak
  proxy-routed savings vs unavailable attribution.
- Never claim TokenPak savings for non-proxy-routed sessions.

Driven via CODEX_HOME so `default_config_path()` resolves to a temp config.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tokenpak.companion.codex import doctor


def _write_config(home: Path, body: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(body, encoding="utf-8")


def test_warns_when_no_config(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))  # no config.toml written
    status, detail = doctor.check_proxy_routing()
    assert status == "WARN"
    assert "not proxy-routed" in detail
    assert "observability-only" in detail
    assert "unavailable" in detail


def test_warns_when_model_provider_unset(monkeypatch, tmp_path):
    _write_config(tmp_path, 'model = "gpt-5-codex"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    status, detail = doctor.check_proxy_routing()
    assert status == "WARN"
    assert "model_provider unset" in detail
    assert "provider-native" in detail


def test_warns_when_non_tokenpak_provider(monkeypatch, tmp_path):
    _write_config(
        tmp_path,
        'model_provider = "openai"\n'
        "[model_providers.openai]\n"
        'base_url = "https://api.openai.com/v1"\n',
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    status, detail = doctor.check_proxy_routing()
    assert status == "WARN"
    assert "not proxy-routed" in detail


def test_warns_when_tokenpak_selected_but_undefined(monkeypatch, tmp_path):
    # Selected a TokenPak provider name but never defined the block — not
    # actually routed, so the doctor must not over-claim.
    _write_config(tmp_path, 'model_provider = "tokenpak-openai-codex"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    status, detail = doctor.check_proxy_routing()
    assert status == "WARN"


def test_passes_when_proxy_routed(monkeypatch, tmp_path):
    _write_config(
        tmp_path,
        'model_provider = "tokenpak-openai-codex"\n'
        "[model_providers.tokenpak-openai-codex]\n"
        'base_url = "http://127.0.0.1:8766/v1"\n'
        'wire_api = "responses"\n',
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    status, detail = doctor.check_proxy_routing()
    assert status == "PASS"
    assert "proxy-routed" in detail
    assert "tokenpak-openai-codex" in detail


@pytest.mark.parametrize(
    "body",
    [
        "",  # empty config
        'model_provider = "openai"\n',
        'model_provider = "tokenpak-openai-codex"\n',
        'model_provider = "tokenpak-openai-codex"\n'
        "[model_providers.tokenpak-openai-codex]\n"
        'base_url = "http://127.0.0.1:8766/v1"\n',
    ],
)
def test_routing_check_never_fails(monkeypatch, tmp_path, body):
    _write_config(tmp_path, body)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    status, _ = doctor.check_proxy_routing()
    assert status in ("PASS", "WARN")  # never FAIL — not-routed is supported


def test_truthful_labels_when_not_routed(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))  # no config -> not routed
    status, detail = doctor.check_proxy_routing()
    lowered = detail.lower()
    assert status == "WARN"
    # Distinguishes provider-native from TokenPak attribution, and never
    # positively claims savings on a non-routed session.
    assert "provider-native" in lowered
    assert "unavailable" in lowered
    assert "does not claim savings" in lowered
