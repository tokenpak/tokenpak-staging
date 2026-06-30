"""OpenAI Codex Responses API adapter — routes to chatgpt.com/backend-api.

Extends the standard OpenAI Responses adapter with:
- Default upstream: chatgpt.com/backend-api (ChatGPT OAuth endpoint)
- Detection: /v1/responses with JWT bearer token (not sk- API key)
- Same wire format as openai-responses (Responses API)
- Path rewrite: /v1/responses → /codex/responses (ChatGPT backend path)
- Payload fixup: ensures stream=true, store=false, strips max_output_tokens

This enables TokenPak to proxy Codex subscription traffic with full
compression pipeline support (capsules, vault injection, compaction, etc.)
while routing to the correct ChatGPT backend instead of api.openai.com.

Requires: curl_cffi (pip install curl_cffi) for Cloudflare bypass on
chatgpt.com. Falls back to urllib3 if curl_cffi is unavailable, but
chatgpt.com requests will likely get 403'd by CF in that case.
"""

from __future__ import annotations

import json
from typing import Mapping, Optional

from .canonical import CanonicalRequest
from .openai_responses_adapter import OpenAIResponsesAdapter

# ---------------------------------------------------------------------------
# ChatGPT OAuth tokens are JWTs (start with "eyJ").
# OpenAI API keys start with "sk-".
# This heuristic lets us auto-detect which upstream to use without
# requiring any user configuration — zero-config, just works.
# ---------------------------------------------------------------------------

def _is_chatgpt_oauth_token(auth_header: str) -> bool:
    """Return True if the Authorization header carries a ChatGPT OAuth JWT."""
    if not auth_header:
        return False
    # Strip "Bearer " prefix
    token = auth_header
    lower = auth_header.lower()
    if lower.startswith("bearer "):
        token = auth_header[7:].strip()
    if not token:
        return False
    # ChatGPT OAuth tokens are JWTs: base64url header.payload.signature
    # They always start with "eyJ" (base64 of '{"')
    # API keys start with "sk-"
    return token.startswith("eyJ") and "." in token


def codex_responses_payload_fixup(body: bytes) -> bytes:
    """Apply the ChatGPT Codex Responses constraints to a request body.

    This mirrors :meth:`OpenAICodexResponsesAdapter.denormalize` but operates
    directly on raw request bytes so the proxy can apply it on the additive
    ``/v1/responses`` → ChatGPT-backend route WITHOUT running the full adapter
    normalize/denormalize round-trip.

    EXACT transformations applied (and nothing else):
      1. ``stream``            → set to ``True``  (ChatGPT backend requires SSE)
      2. ``store``             → set to ``False`` (ChatGPT backend rejects store)
      3. ``max_output_tokens`` → removed (unsupported by the ChatGPT backend)
      4. ``input`` (if a non-empty ``str``) → converted to the Responses list
         form ``[{"role":"user","content":[{"type":"input_text","text":<text>}]}]``

    No other field is read, added, or modified. No prompt/response content is
    persisted. Returns the ORIGINAL bytes unchanged on any exception or when the
    decoded body is not a JSON object (fail-open: never break a request).
    """
    try:
        payload = json.loads(body)
    except Exception:
        return body
    if not isinstance(payload, dict):
        return body
    try:
        payload["stream"] = True
        payload["store"] = False
        payload.pop("max_output_tokens", None)
        if isinstance(payload.get("input"), str):
            text = payload["input"]
            if text:
                payload["input"] = [
                    {"role": "user", "content": [{"type": "input_text", "text": text}]}
                ]
            else:
                payload["input"] = []
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except Exception:
        return body


def _codex_response_completed_usage(sse_bytes: bytes) -> dict[str, int | str]:
    """Extract monitor-safe usage from Codex ``response.completed`` SSE bytes.

    The parser keeps only model and integer usage counters. It never persists
    prompt or response content and returns zero counters on malformed input.
    """
    result: dict[str, int | str] = {
        "model": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
    }
    try:
        lines = sse_bytes.decode("utf-8", errors="replace").split("\n")
    except Exception:
        return result

    for line in lines:
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue

        response = event.get("response")
        response_obj = response if isinstance(response, dict) else {}
        model = response_obj.get("model") or event.get("model")
        if isinstance(model, str) and model:
            result["model"] = model

        usage = response_obj.get("usage")
        if usage is None:
            usage = event.get("usage")
        if not isinstance(usage, dict):
            continue

        result["input_tokens"] = int(
            usage.get("input_tokens")
            or usage.get("prompt_tokens")
            or 0
        )
        result["output_tokens"] = int(
            usage.get("output_tokens")
            or usage.get("completion_tokens")
            or 0
        )
        details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            result["cache_read_tokens"] = int(details.get("cached_tokens") or 0)

    return result


class OpenAICodexResponsesAdapter(OpenAIResponsesAdapter):
    """Codex Responses adapter — same format, different upstream + detection.

    Key differences from standard OpenAI Responses:
    - Upstream: chatgpt.com/backend-api (not api.openai.com)
    - Path: /codex/responses (not /v1/responses)
    - Requires: stream=true, store=false, no max_output_tokens
    - Uses curl_cffi for Cloudflare bypass
    """

    source_format = "openai-codex-responses"

    # The ChatGPT backend path for Codex
    CODEX_PATH = "/codex/responses"

    def detect(self, path: str, headers: Mapping[str, str], body: Optional[bytes]) -> bool:
        """Match Codex requests by path or by JWT auth.

        Three match cases:
        1. Bare ``/codex/responses`` — sent by clients that use
           the chatgpt.com backend path directly. The proxy injects the
           ChatGPT OAuth token, so we can't rely on the client's auth header.
        2. ``/v1/codex/responses`` — explicit Codex namespace via the
           standard ``/v1`` prefix.
        3. ``/v1/responses`` *with* a ChatGPT OAuth JWT in the Authorization
           header — distinguishes Codex traffic from regular OpenAI API
           traffic on the shared ``/v1/responses`` endpoint.
        """
        if "/codex/responses" in path or "/v1/codex/responses" in path:
            return True
        if "/v1/responses" not in path:
            return False
        # /v1/responses with a ChatGPT OAuth JWT → Codex
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        return _is_chatgpt_oauth_token(auth)

    def get_default_upstream(self) -> str:
        return "https://chatgpt.com/backend-api"

    def get_sse_format(self) -> str:
        return "openai-responses-sse"

    def get_upstream_path(self) -> str:
        """Return the correct path for the ChatGPT Codex backend."""
        return self.CODEX_PATH

    def denormalize(self, canonical: CanonicalRequest) -> bytes:
        """Denormalize with ChatGPT Codex constraints applied.

        The ChatGPT Codex backend requires:
        - stream: true (always)
        - store: false (always)
        - no max_output_tokens parameter
        - input as a list (not string)
        """
        # Use parent denormalize to get the base payload
        base_bytes = super().denormalize(canonical)
        payload = json.loads(base_bytes)

        # Apply ChatGPT Codex constraints
        payload["stream"] = True
        payload["store"] = False

        # Remove unsupported parameters
        payload.pop("max_output_tokens", None)

        # Ensure input is always a list
        if isinstance(payload.get("input"), str):
            text = payload["input"]
            if text:
                payload["input"] = [
                    {"role": "user", "content": [{"type": "input_text", "text": text}]}
                ]
            else:
                payload["input"] = []

        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
