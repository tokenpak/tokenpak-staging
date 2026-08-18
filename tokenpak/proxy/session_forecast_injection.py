# SPDX-License-Identifier: Apache-2.0
"""Opt-in, default-off client-return decoration of session economics.

This module owns a single, narrow concern: appending a one-line rendered
session-economics summary to the *client-facing copy* of a non-streaming
model response, behind a feature flag that defaults to off. Every function
here is fail-open — a failure decorating or scrubbing must never break a
request or change what the provider sees.

Hard invariants (do not relax without updating the governing task packet):

- Disabled (default): every function is a no-op; the client-facing bytes
  are the exact bytes forwarded to/received from the provider.
- Enabled: the decoration is appended ONLY to the bytes written to the
  client. The bytes forwarded to and received from the provider, and every
  accounting/forecast input derived from them, are computed from the
  original, undecorated copy — never from the decorated one.
- The envelope uses a stable, versioned marker so a later turn's echoed
  history can be recognized and scrubbed before it is forwarded upstream.
  Scrubbing is idempotent and a byte-identical no-op whenever no marker is
  present, so the overwhelmingly common (marker-free) request never pays a
  parse/serialize cost or risks a formatting drift.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

_ENV_VAR = "TOKENPAK_SESSION_FORECAST_INJECTION"


def is_injection_enabled() -> bool:
    """Return True if the optional client-return decoration is enabled.

    Resolution order (mirrors every other opt-in proxy flag):
      1. ``TOKENPAK_SESSION_FORECAST_INJECTION`` env var (1/true -> on)
      2. ``~/.tokenpak/config.json`` "session_forecast_injection.enabled" key
      3. Default: False (opt-in — disabled by default)
    """
    env_val = os.environ.get(_ENV_VAR)
    if env_val is not None:
        return env_val not in ("0", "false", "False", "no")
    try:
        from tokenpak.core.config import load_config

        data = load_config()
        cfg = data.get("session_forecast_injection", {})
        if isinstance(cfg, dict):
            return bool(cfg.get("enabled", False))
        return bool(cfg)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Stable marker envelope
# ---------------------------------------------------------------------------

MARKER_OPEN_PREFIX = "[TP-ECON "
MARKER_VERSION = "v=1"
MARKER_CLOSE = "[/TP-ECON]"

# Matches exactly the span appended by ``wrap_marker`` — the leading blank
# line plus one complete envelope. DOTALL so a rendered line (there is only
# ever one) is captured regardless of content.
_MARKER_RE = re.compile(
    r"\n\n\[TP-ECON[^\]]*\]\n.*?\n\[/TP-ECON\]",
    re.DOTALL,
)


def wrap_marker(line: str) -> str:
    """Wrap a single rendered economics line in the stable marker envelope."""
    return f"\n\n{MARKER_OPEN_PREFIX}{MARKER_VERSION}]\n{line}\n{MARKER_CLOSE}"


def contains_marker(text: str) -> bool:
    """Cheap substring check — safe to call on arbitrary text."""
    return MARKER_OPEN_PREFIX in text


def strip_markers(text: str) -> str:
    """Idempotently remove every injected envelope from ``text``.

    A no-op (returns ``text`` unchanged, including identity for callers
    that check ``is`` rather than ``==``) when no marker is present.
    """
    if MARKER_OPEN_PREFIX not in text:
        return text
    return _MARKER_RE.sub("", text)


# ---------------------------------------------------------------------------
# Client-return decoration (response side)
# ---------------------------------------------------------------------------


def decorate_response_body(resp_body: bytes, economics_line: str) -> bytes:
    """Return a NEW bytes object with the marker envelope appended to the
    last text block of a non-streaming Anthropic-shaped response.

    ``resp_body`` itself is never mutated — callers must keep using the
    original object for accounting/metrics. Returns ``resp_body`` unchanged
    (same object) on any shape mismatch or parse failure — fail-open.
    """
    try:
        data = json.loads(resp_body)
    except Exception:
        return resp_body
    content = data.get("content")
    if not isinstance(content, list):
        return resp_body
    for block in reversed(content):
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            block["text"] = block["text"] + wrap_marker(economics_line)
            try:
                return json.dumps(data).encode()
            except Exception:
                return resp_body
    return resp_body


def maybe_decorate_response(
    resp_body: bytes,
    *,
    session_id: str,
    db_path: Any,
    model_hint: str = "",
    now: Any = None,
) -> bytes:
    """Best-effort, fail-open client-return decoration.

    Returns ``resp_body`` UNCHANGED whenever injection is disabled, no
    ``session_id`` is resolvable, or building/rendering the economics
    summary fails for any reason. Never raises, never performs upstream
    I/O, and never mutates the ledger — the economics read underneath is
    the same zero-side-effect path used by the session-economics endpoint.
    """
    if not session_id or not is_injection_enabled():
        return resp_body
    try:
        from tokenpak.core.contracts.session_economics_renderer import render_line
        from tokenpak.proxy.forecast_endpoint import _build_session_economics_response

        economics = _build_session_economics_response(
            session_id, db_path, model_hint=model_hint, now=now
        )
        line = render_line(economics)
        return decorate_response_body(resp_body, line)
    except Exception:
        logger.debug("session forecast injection failed", exc_info=True)
        return resp_body


# ---------------------------------------------------------------------------
# Round-trip scrub (request side)
# ---------------------------------------------------------------------------


def scrub_request_body(body: bytes) -> bytes:
    """Strip any injected envelope from echoed assistant history before a
    request is forwarded upstream.

    A byte-identical no-op (returns the SAME ``body`` object) whenever the
    marker prefix is not present anywhere in the raw bytes — the
    overwhelmingly common case, including every request while the feature
    is disabled. Only when the marker is actually found does this parse,
    strip, and re-serialize the JSON body. Never raises: any parse failure
    forwards the original bytes unchanged — fail-open, and correct, since a
    body we cannot parse cannot contain a marker we can safely remove.
    """
    if MARKER_OPEN_PREFIX.encode() not in body:
        return body
    try:
        data = json.loads(body)
    except Exception:
        return body
    changed = False
    for message in data.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and contains_marker(content):
            message["content"] = strip_markers(content)
            changed = True
        elif isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, dict)
                    and isinstance(block.get("text"), str)
                    and contains_marker(block["text"])
                ):
                    block["text"] = strip_markers(block["text"])
                    changed = True
    if not changed:
        return body
    try:
        return json.dumps(data).encode()
    except Exception:
        return body


__all__ = [
    "MARKER_CLOSE",
    "MARKER_OPEN_PREFIX",
    "MARKER_VERSION",
    "contains_marker",
    "decorate_response_body",
    "is_injection_enabled",
    "maybe_decorate_response",
    "scrub_request_body",
    "strip_markers",
    "wrap_marker",
]
