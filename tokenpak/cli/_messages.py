# SPDX-License-Identifier: Apache-2.0
"""Centralised CLI message prefixes — the single source for user-facing status.

CLI status lines use **capitalised plain prefixes** (``Error:``, ``Warning:``,
``Info:``) with **no emoji**, per the ratified CLI error-prefix style. Helpers
return the prefixed string so existing call sites can route through ``print()``
or ``click.echo()`` without changing their I/O:

    from tokenpak.cli._messages import error, warn, info

    print(error("config not found"))     # -> "Error: config not found"
    print(warn("budget at 90%"))         # -> "Warning: budget at 90%"
    print(info("vault loaded"))          # -> "Info: vault loaded"

Routing every CLI error/warn/info line through these helpers keeps the prefix
style consistent across the surface and makes a future style change a one-file
edit.
"""

from __future__ import annotations

# Capitalised plain prefixes — no emoji, no ANSI. The single source of truth.
ERROR_PREFIX = "Error:"
WARNING_PREFIX = "Warning:"
INFO_PREFIX = "Info:"


def error(message: str) -> str:
    """Return *message* with the standard capitalised-plain error prefix."""
    return f"{ERROR_PREFIX} {message}"


def warn(message: str) -> str:
    """Return *message* with the standard capitalised-plain warning prefix."""
    return f"{WARNING_PREFIX} {message}"


def info(message: str) -> str:
    """Return *message* with the standard capitalised-plain info prefix."""
    return f"{INFO_PREFIX} {message}"
