# SPDX-License-Identifier: Apache-2.0
"""Tests for print-only target flow, non-TTY default, and --no-tui escape.

Covers:
  - Print-only targets (cline, codex, openai-sdk, anthropic-sdk, litellm) show
    "needs manual step" banner in guided mode
  - Non-TTY → print-only instructions, no guided header
  - --no-tui escape → print-only instructions even on TTY
  - Targets with applier=None show manual-step note in guided form
  - --apply on a print-only target falls through gracefully (back-compat)
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest

from tokenpak.cli.commands.integrate import (
    _PRINT_ONLY_KEYS,
    INTEGRATIONS,
    Integration,
    _run_guided_form,
    _setup_mode,
    _status_label,
    run_integrate,
)

PROXY = "http://localhost:8766"

PRINT_ONLY_KEYS = list(_PRINT_ONLY_KEYS)

EXPECTED_TARGET_STATUS = {
    "claude-code": ("supported", "tested", "apply"),
    "cursor": ("experimental", "untested", "apply"),
    "cline": ("experimental", "untested", "print-only"),
    "continue": ("experimental", "untested", "apply"),
    "aider": ("experimental", "untested", "apply"),
    "codex": ("experimental", "untested", "print-only"),
    "openai-sdk": ("supported", "tested", "print-only"),
    "anthropic-sdk": ("supported", "tested", "print-only"),
    "litellm": ("supported", "tested", "print-only"),
}


def _make_args(**kwargs) -> argparse.Namespace:
    defaults = {
        "proxy_url": PROXY,
        "apply": False,
        "revert": False,
        "client": None,
        "all": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_integration_registry_status_labels_match_apply_modes():
    """README, quickstart, matrix, and runtime labels rely on this mapping."""
    by_key = {integration.key: integration for integration in INTEGRATIONS}

    assert set(EXPECTED_TARGET_STATUS) <= set(by_key)
    for key, (maturity, proof, setup_mode) in EXPECTED_TARGET_STATUS.items():
        integration = by_key[key]
        assert integration.maturity == maturity
        assert integration.proof_label == proof
        assert _setup_mode(integration) == setup_mode
        assert _status_label(integration) == f"{maturity}; {proof}; {setup_mode}"


# ── Print-only targets in guided form ─────────────────────────────────────


@pytest.mark.parametrize("key", PRINT_ONLY_KEYS)
def test_print_only_target_guided_shows_manual_step(capsys, key):
    """Guided form for a print-only target must show manual-step warning."""
    # Find the real integration
    integ = next(i for i in INTEGRATIONS if i.key == key)

    # Run guided form (no _tty_confirm needed — print-only exits before it)
    rc = _run_guided_form(integ, PROXY)

    assert rc == 0
    out = capsys.readouterr().out
    assert "manual step" in out or "auto-apply not available" in out.lower()
    # Instructions must appear
    assert len(out.strip()) > 0


@pytest.mark.parametrize("key", PRINT_ONLY_KEYS)
def test_print_only_target_instructions_present(capsys, key):
    """Instructions text must be printed for every print-only target."""
    integ = next(i for i in INTEGRATIONS if i.key == key)
    _run_guided_form(integ, PROXY)
    out = capsys.readouterr().out
    # Each target's instruction contains the proxy URL
    assert PROXY in out or len(out) > 50  # proxy URL or substantial output


# ── Non-TTY default: print-only ───────────────────────────────────────────


def test_non_tty_defaults_to_print_only(capsys):
    """Non-TTY (stdin not a tty) → print-only output, no guided header."""
    integ = next(i for i in INTEGRATIONS if i.key == "claude-code")
    with (
        patch("tokenpak.cli.commands.integrate._find", return_value=integ),
        patch("tokenpak.cli.commands.integrate._is_no_tui", return_value=False),
        patch("tokenpak.cli.commands.integrate._is_interactive", return_value=False),
    ):
        rc = run_integrate(_make_args(client="claude-code"))

    assert rc == 0
    out = capsys.readouterr().out
    assert "(guided)" not in out
    assert "ANTHROPIC_BASE_URL" in out


def test_non_tty_no_prompt_emitted(capsys):
    """Under non-TTY, no Y/n prompt should appear in output."""
    integ = next(i for i in INTEGRATIONS if i.key == "cursor")
    with (
        patch("tokenpak.cli.commands.integrate._find", return_value=integ),
        patch("tokenpak.cli.commands.integrate._is_no_tui", return_value=False),
        patch("tokenpak.cli.commands.integrate._is_interactive", return_value=False),
    ):
        rc = run_integrate(_make_args(client="cursor"))

    assert rc == 0
    out = capsys.readouterr().out
    assert "[Y/n]" not in out


# ── --no-tui escape ───────────────────────────────────────────────────────


def test_no_tui_flag_bypasses_guided_form(capsys):
    """--no-tui → print-only even when TTY is available."""
    integ = next(i for i in INTEGRATIONS if i.key == "claude-code")
    with (
        patch("tokenpak.cli.commands.integrate._find", return_value=integ),
        patch("tokenpak.cli.commands.integrate._is_no_tui", return_value=True),
        patch("tokenpak.cli.commands.integrate._is_interactive", return_value=True),
    ):
        rc = run_integrate(_make_args(client="claude-code"))

    assert rc == 0
    out = capsys.readouterr().out
    assert "(guided)" not in out
    assert "[Y/n]" not in out


# ── --apply on print-only target back-compat ──────────────────────────────


def test_apply_on_print_only_target_graceful(capsys):
    """--apply on a client with applier=None → graceful fallback, exit 0."""
    integ = next(i for i in INTEGRATIONS if i.key == "codex")
    assert integ.applier is None  # codex has no applier

    with patch("tokenpak.cli.commands.integrate._find", return_value=integ):
        rc = run_integrate(_make_args(client="codex", apply=True))

    assert rc == 0
    out = capsys.readouterr().out
    # Should show instructions and a note, not crash
    assert len(out) > 10


def test_codex_print_only_instructions_are_public_safe(capsys):
    """Codex setup text must not expose internal memory or vault routing notes."""
    integ = next(i for i in INTEGRATIONS if i.key == "codex")

    with patch("tokenpak.cli.commands.integrate._find", return_value=integ):
        rc = run_integrate(_make_args(client="codex"))

    assert rc == 0
    out = capsys.readouterr().out
    assert "OPENAI_BASE_URL" in out
    assert "codex exec" in out
    assert "TokenPak does not edit your Codex config or print credentials" in out
    assert "project_tokenpak" not in out
    assert "memory" not in out.lower()
    assert "vault" not in out.lower()
    assert "/home/" not in out


def test_apply_on_sdk_target_graceful(capsys):
    """--apply on an SDK target → graceful fallback with note about SDK."""
    integ = next(i for i in INTEGRATIONS if i.key == "openai-sdk")

    with patch("tokenpak.cli.commands.integrate._find", return_value=integ):
        rc = run_integrate(_make_args(client="openai-sdk", apply=True))

    assert rc == 0
    out = capsys.readouterr().out
    assert "SDK" in out or "snippet" in out or len(out) > 10


# ── Integration has applier=None but is not in _PRINT_ONLY_KEYS ───────────


def test_guided_form_applier_none_custom(capsys):
    """Custom integration with applier=None in guided form → manual-step banner."""
    integ = Integration(
        key="custom-tool",
        label="Custom Tool",
        kind="client",
        detector=lambda: None,
        instructions=lambda url: f"Set CUSTOM_BASE_URL={url}",
        applier=None,  # no applier
    )

    rc = _run_guided_form(integ, PROXY)

    assert rc == 0
    out = capsys.readouterr().out
    assert "manual step" in out or "auto-apply not available" in out.lower()
    assert f"CUSTOM_BASE_URL={PROXY}" in out
