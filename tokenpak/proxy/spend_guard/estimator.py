# SPDX-License-Identifier: Apache-2.0
"""Token + cost projection for spend guard preflight.

Single source of truth for pricing is :mod:`tokenpak.models` — we never
duplicate the rate map (pricing stays dynamic).

Token estimation is intentionally cheap (char//4 heuristic on the JSON body
plus an Anthropic-shaped messages walk to find the cached/uncached split).
The estimator runs on every inbound request, so it must stay sub-millisecond.
"""

from __future__ import annotations

import json

from .contracts import RiskEstimate

# char-per-token heuristic. Anthropic tokens are ~3.6 chars on average
# for English; using 4 keeps us conservative-on-the-low-side.
_CHARS_PER_TOKEN = 4

# Default max_tokens when caller didn't set one. Anthropic API requires
# max_tokens to be set, so this is mostly a safety net for malformed
# requests (output cost is bounded by what the model is allowed to produce).
_DEFAULT_OUTPUT_TOKENS = 4096

# Cap on the number of context contributors surfaced in the proof — enough to
# name what's actually filling the window without bloating the receipt.
_MAX_CONTRIBUTORS = 5


def _rank_contributors(raw: list, total: int) -> list:
    """Rank ``(source_label, tokens)`` pairs biggest-first with a pct share.

    Returns up to :data:`_MAX_CONTRIBUTORS` receipt-ready dicts, each
    ``{"source": str, "tokens": int, "pct": float}`` where ``pct`` is the
    contributor's share of ``total`` (rounded to 4 dp; 0.0 when total is 0).
    """
    ranked = sorted(raw, key=lambda p: p[1], reverse=True)[:_MAX_CONTRIBUTORS]
    out: list = []
    for label, toks in ranked:
        pct = round(toks / total, 4) if total > 0 else 0.0
        out.append({"source": label, "tokens": int(toks), "pct": pct})
    return out


def _count_text_tokens(text: str) -> int:
    """Cheap character-based token count.

    Returns 0 for empty/None inputs. Used both for the new request body and
    for prior cached context size estimation.
    """
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _walk_anthropic_body(body_json: dict) -> tuple[int, int, float, list]:
    """Walk an Anthropic ``/v1/messages`` body.

    Returns ``(current_context_tokens, request_tokens, cache_hit_ratio,
    contributors)``:
    - current_context_tokens: tokens in messages that look cached
      (any block with ``cache_control`` set, or the first system block)
    - request_tokens: tokens in messages that don't carry a cache marker —
      these are "new" content to be tokenized fresh.
    - cache_hit_ratio: cached / total, 0.0..1.0.
    - contributors: ranked top context contributors (system / tools / each
      message), biggest-first, as receipt-ready dicts. The token accounting
      below is unchanged — contributors are a read-only by-product.

    Best-effort. On malformed input, returns (0, total, 0.0, []) so the
    estimator treats the entire body as fresh — that's the safer (more
    expensive) projection.
    """
    cached = 0
    fresh = 0
    # (source_label, tokens) pairs, ranked into the proof at the end.
    raw_contributors: list = []

    # System block(s)
    sys_block = body_json.get("system")
    sys_tokens = 0
    if isinstance(sys_block, str):
        # String system prompt — Anthropic auto-caches large ones, so we
        # treat it as cached.
        sys_tokens = _count_text_tokens(sys_block)
        cached += sys_tokens
    elif isinstance(sys_block, list):
        for blk in sys_block:
            if not isinstance(blk, dict):
                continue
            txt = blk.get("text", "") or ""
            # With or without explicit cache_control, system blocks tend to be
            # cached implicitly by Anthropic for repeat content.
            n = _count_text_tokens(txt)
            sys_tokens += n
            cached += n
    if sys_tokens:
        raw_contributors.append(("system", sys_tokens))

    # Messages
    for idx, msg in enumerate(body_json.get("messages", []) or []):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "?"))
        msg_tokens = 0
        content = msg.get("content")
        if isinstance(content, str):
            n = _count_text_tokens(content)
            msg_tokens += n
            fresh += n
        elif isinstance(content, list):
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                txt = blk.get("text", "") or ""
                # Tool result / image / other blocks — count their text bag.
                if not txt:
                    # tool_use input/output blocks live in JSON form
                    txt = json.dumps(blk.get("input") or blk.get("content") or "", default=str)
                n = _count_text_tokens(txt)
                msg_tokens += n
                if blk.get("cache_control"):
                    cached += n
                else:
                    fresh += n
        if msg_tokens:
            raw_contributors.append((f"message[{idx}]:{role}", msg_tokens))

    # Tools schema (large in long agent loops)
    tools = body_json.get("tools") or []
    if isinstance(tools, list) and tools:
        # Tools are stable across a session — almost always cached.
        tool_tokens = _count_text_tokens(json.dumps(tools, default=str))
        cached += tool_tokens
        if tool_tokens:
            raw_contributors.append(("tools", tool_tokens))

    total = cached + fresh
    contributors = _rank_contributors(raw_contributors, total)
    if total == 0:
        return 0, 0, 0.0, contributors
    ratio = cached / total
    return cached, fresh, ratio, contributors


def estimate(
    body: bytes,
    model: str,
    *,
    fallback_session_tokens: int = 0,
) -> RiskEstimate:
    """Project tokens + cost for a single provider-bound request.

    Parameters
    ----------
    body : bytes
        Raw request body. Decoded best-effort as UTF-8 JSON.
    model : str
        Resolved model id (e.g. ``claude-opus-4-7``).
    fallback_session_tokens : int
        If we can't parse the body, use this as a floor for context size.
        Typically the proxy's last-known cache_read for this session.
    """
    # Pricing — always dynamic (read from tokenpak.models)
    try:
        from tokenpak.models import get_rates
        rates = get_rates(model)
    except Exception:
        # Most-expensive frontier-class default if the registry is wedged.
        # Better to over-estimate and warn than silently under-block.
        rates = {"input": 15.0, "output": 75.0, "cached": 1.50}

    # Parse body. Track whether it parsed as a JSON object so we can mark an
    # unparseable request as an *unknown* estimate (vs. a confident zero).
    body_json: dict = {}
    body_text = ""
    parse_ok = False
    try:
        body_text = body.decode("utf-8", errors="replace")
        body_json = json.loads(body_text)
        parse_ok = isinstance(body_json, dict)
    except Exception:
        body_json = {}
        parse_ok = False

    contributors: list = []
    contributors_reason: str | None = None
    estimate_unknown = False
    unknown_reason: str | None = None

    if parse_ok and body_json and "messages" in body_json:
        cached, fresh, hit_ratio, contributors = _walk_anthropic_body(body_json)
        if not contributors:
            contributors_reason = "empty_request"
    else:
        # Unknown shape — fall back to whole-body fresh.
        total = _count_text_tokens(body_text)
        cached = max(0, fallback_session_tokens)
        fresh = total
        denom = cached + fresh
        hit_ratio = (cached / denom) if denom else 0.0
        if not parse_ok:
            # We could not parse the body at all. The bands still run on this
            # crude whole-body fallback (so a giant unparseable blob still
            # trips the token-fallback ceilings), but we mark the estimate
            # unknown so the policy emits a warn instead of a silent allow.
            estimate_unknown = True
            unknown_reason = "unparseable_request_body"
            contributors_reason = "unparseable_request_body"
        else:
            # Valid JSON but not an Anthropic ``/v1/messages`` shape — we have a
            # crude size estimate but no per-section contributor breakdown.
            contributors_reason = "non_messages_request_shape"

    projected_input = cached + fresh
    projected_output = int((body_json.get("max_tokens") if parse_ok else None) or _DEFAULT_OUTPUT_TOKENS)

    # Cost components: cached input gets the ``cached`` rate (~10% of input),
    # fresh input gets full input rate, output gets output rate.
    in_per_mtok = float(rates.get("input", 3.0))
    out_per_mtok = float(rates.get("output", 15.0))
    cached_per_mtok = float(rates.get("cached", in_per_mtok * 0.1))

    cost = (
        cached * cached_per_mtok / 1_000_000
        + fresh * in_per_mtok / 1_000_000
        + projected_output * out_per_mtok / 1_000_000
    )

    return RiskEstimate(
        model=model,
        current_context_tokens=cached,
        request_tokens=fresh,
        projected_input_tokens=projected_input,
        projected_output_tokens=projected_output,
        projected_cost_usd=round(cost, 6),
        cache_hit_ratio=round(hit_ratio, 4),
        rates={
            "input": in_per_mtok,
            "output": out_per_mtok,
            "cached": cached_per_mtok,
        },
        contributors=contributors,
        contributors_reason=contributors_reason,
        estimate_unknown=estimate_unknown,
        unknown_reason=unknown_reason,
    )
