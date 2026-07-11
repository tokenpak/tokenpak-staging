# SPDX-License-Identifier: Apache-2.0
"""Internal request traffic classifier.

Attributes every proxied request to exactly one of three canonical classes
(``managed`` / ``raw_claude_observed`` / ``external_untagged``) using the strict
Defined detection precedence drives spend-guard accounting and audit attribution.

The classifier is read-only on the request: the only output is the attributed
class + detection reason + agent attribution. It NEVER mutates the body, NEVER
reads OS process env at request time, and NEVER infers the class from
URL / port / remote IP. Env-derived markers reach the classifier only via a
launcher-synthesised internal header, never by reading
``os.environ`` here.

``__all__`` is intentionally empty: this is internal proxy plumbing, not released
public API. Every consumer imports the specific name it needs function-locally so
these helpers stay off the public API surface.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Canonical request classes. Stable string literals — no synonyms,
#    no plurals, no aliasing. These three values are the canonical set.
MANAGED = "managed"
RAW_CLAUDE_OBSERVED = "raw_claude_observed"
EXTERNAL_UNTAGGED = "external_untagged"

REQUEST_CLASSES = frozenset({MANAGED, RAW_CLAUDE_OBSERVED, EXTERNAL_UNTAGGED})

# ── Canonical detection reasons. One per precedence rung; persisted
#    alongside the class on every audit row for forensic reconstruction. No
#    synonyms (``header-agent`` / ``agentHeader`` are forbidden).
HEADER_AGENT = "header_agent"
HEADER_MANAGED = "header_managed"
ENV_LAUNCHER = "env_launcher"
UA_CLAUDE_CODE = "ua_claude_code"
NO_MARKER = "no_marker"

DETECTION_REASONS = frozenset(
    {HEADER_AGENT, HEADER_MANAGED, ENV_LAUNCHER, UA_CLAUDE_CODE, NO_MARKER}
)

# ── Header names (lowercase for case-insensitive matching).
HEADER_NAME_AGENT = "x-tokenpak-agent"
HEADER_NAME_MANAGED = "x-tokenpak-managed"
# The launcher synthesises this internal header from ``TOKENPAK_MANAGED=1`` in
# the producer's process env. The classifier reads the header; it never reads
# ``os.environ`` itself.
HEADER_NAME_MANAGED_ENV = "x-tokenpak-managed-env"

# TokenPak-internal markers that MUST be stripped before upstream forwarding
# (re-synthesised at each
# hop, never passed through, even to a TokenPak-managed downstream proxy).
INTERNAL_MANAGED_HEADERS = frozenset(
    {HEADER_NAME_AGENT, HEADER_NAME_MANAGED, HEADER_NAME_MANAGED_ENV}
)

# Canonical Claude Code CLI User-Agent substring.
# Matched case-insensitively, and ONLY after every higher-precedence marker has
# failed. Single source of truth — do not duplicate this literal elsewhere.
CLAUDE_CODE_UA_SUBSTRING = "claude-cli"

# "managed marker present, but no agent name to attribute" sentinel (rung 2
# and rung 3). Empty string, never fabricated.
UNKNOWN_MANAGED_AGENT = ""

# Tokens that count as an opt-in "on" for the markerless managed headers. An
# explicit ``0`` / ``false`` (or absence) does NOT mark a request managed.
_TRUTHY_MARKERS = frozenset({"1", "true", "yes", "on"})

# Intentionally empty — keeps every name above off the public-API snapshot.
__all__: list[str] = []


@dataclass(frozen=True)
class Classification:
    """Result of classifying one request — read-only attribution.

    ``request_class`` is one of :data:`REQUEST_CLASSES`; ``reason`` is one of
    :data:`DETECTION_REASONS`; ``agent_attribution`` is the lower-cased
    ``X-Tokenpak-Agent`` value for ``header_agent`` matches, else ``""``.
    """

    request_class: str
    reason: str
    agent_attribution: str = ""


def _header(headers, name: str) -> str:
    """Case-insensitive header lookup → stripped string value (or "")."""
    if not headers:
        return ""
    try:
        items = headers.items()
    except AttributeError:
        return ""
    target = name.lower()
    for key, value in items:
        if str(key).lower() == target:
            return str(value).strip()
    return ""


def _is_truthy_marker(value: str) -> bool:
    """A managed marker is opt-in: present and set to a truthy token."""
    return value.strip().lower() in _TRUTHY_MARKERS


def classify(headers) -> Classification:
    """Attribute a request via the defined precedence chain.

    Evaluates markers in strict order, returning on the first match:

      1. ``X-Tokenpak-Agent: <name>``  → ``managed`` (reason ``header_agent``),
         agent attribution = the lower-cased header value.
      2. ``X-Tokenpak-Managed: 1``     → ``managed`` (reason ``header_managed``),
         agent attribution = unknown-managed.
      3. ``X-Tokenpak-Managed-Env: 1`` → ``managed`` (reason ``env_launcher``),
         agent attribution = launcher-managed.
      4. Claude Code UA substring      → ``raw_claude_observed`` (reason
         ``ua_claude_code``) when no higher marker matched.
      5. otherwise                     → ``external_untagged`` (reason
         ``no_marker``).

    Read-only: never mutates ``headers``, never consults ``os.environ``, never
    infers from URL / port / remote IP.
    """
    agent = _header(headers, HEADER_NAME_AGENT)
    if agent:
        return Classification(MANAGED, HEADER_AGENT, agent.lower())

    if _is_truthy_marker(_header(headers, HEADER_NAME_MANAGED)):
        return Classification(MANAGED, HEADER_MANAGED, UNKNOWN_MANAGED_AGENT)

    if _is_truthy_marker(_header(headers, HEADER_NAME_MANAGED_ENV)):
        return Classification(MANAGED, ENV_LAUNCHER, UNKNOWN_MANAGED_AGENT)

    ua = _header(headers, "user-agent").lower()
    if ua and CLAUDE_CODE_UA_SUBSTRING in ua:
        return Classification(RAW_CLAUDE_OBSERVED, UA_CLAUDE_CODE, "")

    return Classification(EXTERNAL_UNTAGGED, NO_MARKER, "")


def strip_managed_headers(headers) -> list[str]:
    """Remove TokenPak-internal managed markers from a forward-bound header map.

    Mutates ``headers`` in place (case-insensitive) and returns the list of
    header names actually removed. These
    markers are re-synthesised at each hop and MUST NEVER be forwarded upstream —
    even to a TokenPak-managed downstream proxy. A no-op (returns ``[]``) when
    none are present or ``headers`` is not a mutable mapping.
    """
    removed: list[str] = []
    try:
        keys = list(headers.keys())
    except AttributeError:
        return removed
    for key in keys:
        if str(key).lower() in INTERNAL_MANAGED_HEADERS:
            try:
                del headers[key]
            except (KeyError, TypeError):
                continue
            removed.append(key)
    return removed
