# SPDX-License-Identifier: Apache-2.0
"""Tests for the auth-mode helper that gates the direct-API-key warning.

The contract: warning about a missing ``ANTHROPIC_API_KEY`` is warranted
only when no OAuth / proxy / session-auth path covers Anthropic. OAuth
tokens, OAuth-token env vars, and stored BYOK creds suppress the warning;
a plain env-var API key (the direct-key path itself) does not.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from tokenpak.creds.auth_mode import (
    direct_api_key_warning_warranted,
    non_direct_key_auth_available,
)
from tokenpak.creds.model import (
    KIND_API_KEY,
    KIND_BEARER,
    KIND_OAUTH,
    REFRESH_EXTERNAL,
    REFRESH_NONE,
    Credential,
)


def _cred(
    cid: str = "fake",
    platform: str = "anthropic",
    kind: str = KIND_OAUTH,
    provider: str = "claude-cli",
) -> Credential:
    return Credential(
        id=cid,
        platform=platform,
        kind=kind,
        source="fake",
        provider=provider,
        refresh_owner=REFRESH_NONE if kind == KIND_API_KEY else REFRESH_EXTERNAL,
        secret_ref=f"{provider}:{cid}",
    )


def _no_oauth_env():
    # Clear OAuth-token env vars so they don't leak in from the runner's env.
    return patch.dict(
        os.environ,
        {"ANTHROPIC_OAUTH_TOKEN": "", "ANTHROPIC_OAUTH_TOKEN2": ""},
        clear=False,
    )


# ── warning SUPPRESSED in OAuth / session / proxy modes ──────────────


def test_oauth_credential_suppresses_warning():
    """Claude CLI OAuth token covers Anthropic → no direct key needed."""
    with _no_oauth_env():
        creds = [_cred(kind=KIND_OAUTH, provider="claude-cli")]
        assert non_direct_key_auth_available("anthropic", creds=creds) is True
        assert direct_api_key_warning_warranted("anthropic", creds=creds) is False


def test_bearer_credential_suppresses_warning():
    """A session/bearer token also counts as non-direct-key auth."""
    with _no_oauth_env():
        creds = [_cred(kind=KIND_BEARER, provider="openclaw")]
        assert non_direct_key_auth_available("anthropic", creds=creds) is True
        assert direct_api_key_warning_warranted("anthropic", creds=creds) is False


def test_oauth_token_env_var_suppresses_warning():
    """ANTHROPIC_OAUTH_TOKEN (proxy-recognised) suppresses the warning even
    with no discovered creds."""
    with patch.dict(os.environ, {"ANTHROPIC_OAUTH_TOKEN": "oauth-tok"}, clear=False):
        assert non_direct_key_auth_available("anthropic", creds=[]) is True
        assert direct_api_key_warning_warranted("anthropic", creds=[]) is False


def test_byok_stored_key_suppresses_warning():
    """A stored BYOK key (user-config provider) is a non-env-var path."""
    with _no_oauth_env():
        creds = [_cred(kind=KIND_API_KEY, provider="user-config")]
        assert non_direct_key_auth_available("anthropic", creds=creds) is True
        assert direct_api_key_warning_warranted("anthropic", creds=creds) is False


# ── warning WARRANTED when a direct key is genuinely required ────────


def test_no_anthropic_auth_warrants_warning():
    """Nothing covers Anthropic → a direct key really is the way in."""
    with _no_oauth_env():
        assert non_direct_key_auth_available("anthropic", creds=[]) is False
        assert direct_api_key_warning_warranted("anthropic", creds=[]) is True


def test_only_env_pool_api_key_warrants_warning():
    """An env-pool API key IS the direct-key path; it doesn't make the
    direct key 'not required' on its own."""
    with _no_oauth_env():
        creds = [_cred(kind=KIND_API_KEY, provider="env-pool")]
        assert non_direct_key_auth_available("anthropic", creds=creds) is False
        assert direct_api_key_warning_warranted("anthropic", creds=creds) is True


def test_other_platform_creds_do_not_count():
    """An OpenAI OAuth token must not suppress the Anthropic warning."""
    with _no_oauth_env():
        creds = [_cred(platform="openai", kind=KIND_OAUTH, provider="codex-cli")]
        assert non_direct_key_auth_available("anthropic", creds=creds) is False
        assert direct_api_key_warning_warranted("anthropic", creds=creds) is True
