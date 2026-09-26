# SPDX-License-Identifier: Apache-2.0
"""Cache key / scope key derivation for the semantic cache stage.

Four helpers:

    extract_query_text(ctx)     — pull normalized text from context; never stores raw prompts
    extract_model_features(ctx) — model/provider identity for the cache-key ``key_features`` dimension
    make_scope_key(ctx)         — session-scoped key for SemanticCache
    is_streaming(ctx)           — True when the request asks for a streaming response

Design constraints (per proposal Component C):
- Do not store raw prompt text; only hashed/normalized forms.
- Scope defaults to session; fall back to platform or request_id.
- Key is stable across semantically equivalent requests.
- Key must be scoped to the addressed model/provider — text similarity alone
  is not sufficient isolation across a multi-model session (see
  ``extract_model_features``).
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .context import OptimizationContext

_WS = re.compile(r"\s+")


def extract_query_text(ctx: "OptimizationContext") -> str:
    """Return a normalized query string from *ctx* without storing raw prompts.

    Sources tried in order:
    1. ``ctx.canonical.messages`` — concatenated text of all message parts.
    2. Raw body decoded as JSON → ``input`` or ``messages`` field.
    3. Empty string (cache lookup will miss; miss is safe).
    """
    # --- 1. Canonical messages ---
    canonical = ctx.canonical
    if canonical is not None:
        messages = getattr(canonical, "messages", None)
        if messages:
            parts = []
            for msg in messages:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if isinstance(content, str):
                    parts.append(f"{role}: {content}")
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            text = block.get("text", "")
                            if text:
                                parts.append(text)
            text = " ".join(parts)
            return _normalize(text)

    # --- 2. Raw body fallback ---
    try:
        data = json.loads(ctx.raw_body or b"")
        # OpenAI Responses API uses "input"; OpenAI Chat uses "messages"
        raw: Any = data.get("input") or data.get("messages")
        if isinstance(raw, str):
            return _normalize(raw)
        if isinstance(raw, list):
            parts = []
            for item in raw:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    c = item.get("content", "")
                    if isinstance(c, str):
                        parts.append(c)
                    elif isinstance(c, list):
                        for b in c:
                            if isinstance(b, dict):
                                parts.append(b.get("text", ""))
            return _normalize(" ".join(parts))
    except Exception:
        pass

    return ""


def extract_model_features(ctx: "OptimizationContext") -> dict[str, str]:
    """Return the model/provider identity of *ctx* as a semantic-cache key dimension.

    Without this, ``SemanticCache`` keys purely on normalized query text +
    wire format. Within one session, a request addressed to model B can then
    silently receive model A's cached response merely because the text is
    near-identical — cross-model response-reuse leakage. Callers pass the
    returned dict as ``key_features`` to ``SemanticCache.lookup`` / ``.store``
    so model/provider identity becomes part of the composite cache key.

    Sources tried in order:
    1. ``ctx.canonical.model`` (+ ``ctx.canonical.source_format`` for the
       provider/adapter identity of the wire format the request arrived in)
       — already extracted by the format adapter that normalized the request.
    2. Raw body decoded as JSON → ``model`` field (provider left unknown).
    3. Empty dict when neither is available — the request then falls into
       the same "no model identity known" bucket as every other caller that
       never supplied one; a miss in that case is safe (identical to
       pre-existing behavior), so this is a conservative fallback, not a
       correctness gap for requests that do carry a ``model`` field.
    """
    canonical = ctx.canonical
    if canonical is not None:
        model = getattr(canonical, "model", "") or ""
        if model:
            provider = getattr(canonical, "source_format", "") or ""
            return {"model": str(model), "provider": str(provider)}

    try:
        data = json.loads(ctx.raw_body or b"")
        model = data.get("model") if isinstance(data, dict) else None
        if isinstance(model, str) and model:
            return {"model": model, "provider": ""}
    except Exception:
        pass

    return {}


def make_scope_key(ctx: "OptimizationContext") -> str:
    """Return a session-scoped key for ``SemanticCache``.

    Priority:
    1. X-Session-Id / X-Claude-Code-Session-Id / X-OpenClaw-Session header.
    2. ``ctx.platform`` + ``ctx.request_id`` composite (weak isolation).
    3. ``ctx.request_id`` alone (request-scoped; every request is a miss).
    """
    headers = ctx.headers or {}
    for h in (
        "x-session-id",
        "x-claude-code-session-id",
        "x-openclaw-session",
        "x-request-session",
    ):
        val = headers.get(h) or headers.get(h.title()) or headers.get(h.upper())
        if val:
            return val

    if ctx.platform:
        return f"{ctx.platform}:{ctx.request_id}"

    return ctx.request_id


def is_streaming(ctx: "OptimizationContext") -> bool:
    """True when the request body requests a streaming response."""
    try:
        data = json.loads(ctx.raw_body or b"")
        return bool(data.get("stream"))
    except Exception:
        return False


def _normalize(text: str) -> str:
    return _WS.sub(" ", text).strip()
