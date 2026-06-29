# SPDX-License-Identifier: Apache-2.0
"""User-facing guidance for optional Codex CLI integration."""

from __future__ import annotations


def _codex_cli_missing_message(action: str | None = None) -> str:
    """Return actionable, non-fatal guidance when ``codex`` is unavailable."""
    prefix = f"{action}: " if action else ""
    return (
        f"{prefix}Codex CLI not found on PATH. "
        "Install OpenAI Codex CLI, for example `npm install -g @openai/codex`, "
        "then confirm `codex --version` works. "
        "Codex integration is optional; TokenPak Claude Code integration can run without it."
    )
