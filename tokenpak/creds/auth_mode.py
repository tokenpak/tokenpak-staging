# SPDX-License-Identifier: Apache-2.0
"""Resolve whether the active auth mode needs a direct provider API key.

A direct ``ANTHROPIC_API_KEY`` (or equivalent for another platform) is
only required when nothing *else* can authenticate that platform. When an
OAuth token (Claude CLI / OpenClaw / Codex CLI), an OAuth-token env var,
or a stored BYOK credential already covers the platform, prompting the
user to "set ANTHROPIC_API_KEY" is a false alarm.

This module reuses the canonical credential discovery
(:func:`tokenpak.creds.providers.discover_all`) rather than inventing a
parallel notion of auth mode. Discovery is dynamic — it walks every
registered provider — so this stays correct as providers are added.
"""

from __future__ import annotations

import os

from .model import KIND_API_KEY
from .providers import discover_all

# OAuth-token env vars the proxy treats as interchangeable with a direct
# API key when building its Anthropic key pool. Their presence means the
# platform can authenticate without a direct ``*_API_KEY``.
_OAUTH_TOKEN_ENV_VARS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN2"),
}


def _env_has_value(var: str) -> bool:
    return bool(os.environ.get(var, "").strip())


def non_direct_key_auth_available(platform: str, creds=None) -> bool:
    """True when ``platform`` can authenticate *without* a direct API key.

    "Non-direct-key auth" is any of:
      * an OAuth-token env var (e.g. ``ANTHROPIC_OAUTH_TOKEN``),
      * a discovered OAuth or bearer credential (Claude CLI / OpenClaw /
        Codex CLI session tokens), or
      * a stored BYOK credential from the user-config provider.

    A plain ``*_API_KEY`` discovered from the environment (the ``env-pool``
    provider) is the *direct-key* path itself and does not count here.

    ``creds`` is injectable for testing; production callers pass None and
    let discovery run.
    """
    platform = platform.lower()

    for var in _OAUTH_TOKEN_ENV_VARS.get(platform, ()):
        if _env_has_value(var):
            return True

    if creds is None:
        creds = discover_all()

    for cred in creds:
        if cred.platform.lower() != platform:
            continue
        # OAuth / bearer credentials are non-direct-key by definition.
        if cred.kind != KIND_API_KEY:
            return True
        # A stored BYOK static key (user-config) is a non-env-var path; the
        # env-pool provider is the direct ``*_API_KEY`` path itself.
        if cred.provider != "env-pool":
            return True

    return False


def direct_api_key_warning_warranted(platform: str, creds=None) -> bool:
    """True when warning about a missing direct API key for ``platform`` is fair.

    The warning is warranted only when no non-direct-key auth path exists —
    i.e. OAuth / proxy / session-auth modes suppress it.
    """
    return not non_direct_key_auth_available(platform, creds=creds)
