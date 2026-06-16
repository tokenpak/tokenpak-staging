"""Routing service — platform origin extraction, session mapping, and the
credential-injection contract.

Sub-module of ``tokenpak/services/`` (per ``01-architecture-standard.md §1``,
the shared execution backbone at Level 3).

Exposes:
    * :class:`PlatformOrigin` — dataclass returned by extractors.
    * :func:`_openclaw_extract` — Path C reader that resolves the active
      host session file recorded by the local runtime.
    * :class:`CredentialProvider` / :class:`InjectionPlan` — the
      credential-injection contract (§2); the canonical compile
      target for ``provider_adapter`` cards (Std 54 §D).
"""

from tokenpak.services.routing_service.credential_injector import (
    CredentialProvider,
    InjectionPlan,
    RequestShape,
)
from tokenpak.services.routing_service.platform_bridge import (
    ATTRIBUTION_ANONYMOUS_USER_AGENT_ONLY,
    ATTRIBUTION_OPENCLAW_ACTIVE_SESSION_FILE,
    ATTRIBUTION_UNKNOWN,
    PlatformOrigin,
    _openclaw_extract,
    _read_active_json,
)

__all__ = [
    "CredentialProvider",
    "InjectionPlan",
    "PlatformOrigin",
    "RequestShape",
    "_openclaw_extract",
    "_read_active_json",
    "ATTRIBUTION_OPENCLAW_ACTIVE_SESSION_FILE",
    "ATTRIBUTION_ANONYMOUS_USER_AGENT_ONLY",
    "ATTRIBUTION_UNKNOWN",
]
