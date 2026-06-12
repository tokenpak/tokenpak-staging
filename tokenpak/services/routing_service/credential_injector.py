"""Credential injection contract — ``InjectionPlan`` + ``CredentialProvider``.

This module is the canonical compile target for ``provider_adapter`` cards
(Std 54 §D: ``tokenpak.services.routing_service.CredentialProvider``) and the
contract home Std 23 §2 points at. It is a fresh authoring against the current
architecture — the pre-pivot ``canonical-main`` implementation of the same
contract is lineage reference only (Std 23 §2.4 truth-alignment note) and was
deliberately not used as a source.

Contract summary (Std 23):

* ``InjectionPlan`` (§2.1) — seven orthogonal, declarative slots describing
  what the proxy does to a forward request, applied in slot order
  1 → 2 → 3 → 4 → 5 → 6 in the proxy hot path, plus the declarative
  ``request_shape`` field (§2.5 amendment).
* ``CredentialProvider`` (§1, §2.3) — auth + URL routing for one provider
  slug. ``resolve()`` returns a plan, or ``None`` when creds are unavailable
  (graceful skip; never raise for missing creds).

No I/O happens here: this module defines the contract only. Concrete
providers live with their integrations (a compiled ``provider_adapter`` card
names its ``{Name}CredentialProvider`` class per Std 23 §2.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Callable,
    Literal,
    Mapping,
    Optional,
    Protocol,
    runtime_checkable,
)

__all__ = [
    "CredentialProvider",
    "InjectionPlan",
    "RequestShape",
]

#: Declarative request shapes (Std 23 §2.5). ``sse-upgrade`` is reserved.
RequestShape = Literal["http", "ws-upgrade", "sse-upgrade"]

#: Body-aware URL computation: ``(body, headers) -> url or None``.
#: A non-``None`` result wins over ``target_url_override`` (slot 4b > 4a).
TargetUrlResolver = Callable[[bytes, Mapping[str, str]], Optional[str]]

#: Dynamic per-request headers (SigV4, OAuth refresh, ...):
#: ``(body, url, method, headers) -> headers``. Runs last (slot 6) and
#: OVERRIDES ``add_headers`` on key conflict (Std 23 §2.2).
HeaderResolver = Callable[[bytes, str, str, Mapping[str, str]], dict[str, str]]

#: Body mutation before forward (slot 5). Must be declared, never implicit
#: (Std 23 §2.3 — no hidden destructive behavior).
BodyTransform = Callable[[bytes], bytes]


@dataclass(frozen=True)
class InjectionPlan:
    """Declarative description of one provider's forward-request mutation.

    Slots apply in the documented order; the record itself never performs
    I/O and must not depend on external mutable state (Std 23 §2.3 —
    refreshable state belongs inside resolver closures, not here).
    """

    #: Slot 1 — caller headers to remove (lowercase names).
    strip_headers: frozenset[str] = frozenset()
    #: Slot 2 — static headers to add.
    add_headers: Mapping[str, str] = field(default_factory=dict)
    #: Slot 3 — values appended to caller-supplied headers, comma-separated.
    merge_headers: Mapping[str, str] = field(default_factory=dict)
    #: Slot 4a — static URL replacement.
    target_url_override: Optional[str] = None
    #: Slot 4b — body-aware URL computation; non-``None`` wins over 4a.
    #: Receives ``b""`` for handshake requests and must tolerate it.
    target_url_resolver: Optional[TargetUrlResolver] = None
    #: Slot 5 — explicit body mutation. Incompatible with ``ws-upgrade``.
    body_transform: Optional[BodyTransform] = None
    #: Slot 6 — dynamic per-request headers; overrides ``add_headers``
    #: on conflict. Receives ``b""`` bodies on handshakes.
    header_resolver: Optional[HeaderResolver] = None
    #: Plan shape (Std 23 §2.5). ``"http"`` preserves prior behavior.
    request_shape: RequestShape = "http"

    def __post_init__(self) -> None:
        lowered = frozenset(h.lower() for h in self.strip_headers)
        if lowered != frozenset(self.strip_headers):
            object.__setattr__(self, "strip_headers", lowered)
        if self.request_shape == "ws-upgrade" and self.body_transform is not None:
            # Std 23 §2.5: the upgrade handshake carries no transformable
            # body — this combination MUST be rejected at plan-load time.
            raise ValueError(
                "InjectionPlan: body_transform cannot be combined with "
                "request_shape='ws-upgrade' (Std 23 §2.5 — rejected at "
                "plan-load time)"
            )


@runtime_checkable
class CredentialProvider(Protocol):
    """Auth + URL routing for one tokenpak provider slug (Std 23 §1).

    Implementations are named ``{Vendor}{Product}CredentialProvider``
    (Std 23 §2.2, vendor-explicit per §1.1) and are typically registered
    once at module import and reused for the proxy lifetime.
    """

    #: The tokenpak provider slug this provider serves (e.g.
    #: ``"tokenpak-azure-openai"``).
    name: str

    def resolve(self) -> Optional[InjectionPlan]:
        """Read credentials and build the plan for this provider.

        Returns ``None`` — never raises — when credentials or optional
        dependencies are unavailable (Std 23 §2.3: graceful skip; a
        crashing provider would break startup for every other provider).
        """
        ...
