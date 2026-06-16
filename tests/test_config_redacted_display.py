"""Tests for the masked display view of agent config — ``redacted_config()``.

Covers the security contract from the get_config display-path masking fix:
secret-class keys are masked at the display/read path, safe keys pass through,
and the raw ``get_config()`` accessor is unchanged so runtime credential
consumers keep working. The masker is the single shared one in
``cli.commands.config_env`` (no parallel implementation).
"""

import pytest

from tokenpak.cli.commands.config_env import mask_value, secret_class
from tokenpak.core import config as core_config

# A representative config view: two secret-class keys (high + medium) and two
# low/safe tuning keys. Values are sentinels we can assert never leak.
_RAW_VIEW = {
    "ANTHROPIC_API_KEY": "sk-ant-deadbeef-SECRET",  # high
    "TELEGRAM_CHAT_ID": "1234567890",  # medium
    "stats_footer": True,  # low / safe
    "metrics.enabled": "1",  # low / safe
}


@pytest.fixture
def fixed_view(monkeypatch):
    """Pin ``get_config`` to a deterministic dict so the test is host-independent."""
    monkeypatch.setattr(core_config, "get_config", lambda: dict(_RAW_VIEW))
    return dict(_RAW_VIEW)


@pytest.mark.quick
def test_redacted_config_masks_secret_class_key(fixed_view):
    redacted = core_config.redacted_config()
    # high secret-class value must be collapsed to the presence sentinel, never
    # any portion of the real secret.
    assert redacted["ANTHROPIC_API_KEY"] == "set"
    assert "deadbeef" not in str(redacted["ANTHROPIC_API_KEY"])
    assert "sk-ant" not in str(redacted["ANTHROPIC_API_KEY"])


@pytest.mark.quick
def test_redacted_config_masks_medium_class_key(fixed_view):
    redacted = core_config.redacted_config()
    assert secret_class("TELEGRAM_CHAT_ID") == "medium"
    assert redacted["TELEGRAM_CHAT_ID"] == "set"
    assert "1234567890" not in str(redacted["TELEGRAM_CHAT_ID"])


@pytest.mark.quick
def test_redacted_config_preserves_safe_key(fixed_view):
    redacted = core_config.redacted_config()
    # low secret-class (safe) tuning values are shown verbatim.
    assert redacted["stats_footer"] is True
    assert redacted["metrics.enabled"] == "1"


@pytest.mark.quick
def test_redacted_config_does_not_mutate_raw_accessor(fixed_view):
    # The raw accessor must remain raw: programmatic/runtime consumers
    # (e.g. the proxy) still read the real secret value.
    assert core_config.get_config()["ANTHROPIC_API_KEY"] == "sk-ant-deadbeef-SECRET"
    # redacted_config must not have mutated the underlying view in place.
    core_config.redacted_config()
    assert core_config.get_config()["ANTHROPIC_API_KEY"] == "sk-ant-deadbeef-SECRET"


@pytest.mark.quick
def test_redacted_config_reuses_shared_masker(fixed_view):
    # Every key's redaction is exactly what the shared mask_value() produces —
    # proving reuse of the single source of truth, not a parallel masker.
    redacted = core_config.redacted_config()
    for key, value in _RAW_VIEW.items():
        assert redacted[key] == mask_value(key, value)
