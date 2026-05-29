# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the additive Codex `/v1/responses` → ChatGPT-backend wiring.

Covers (no network):
  a. `_is_chatgpt_oauth_token` JWT vs sk- discrimination.
  b. `codex_responses_payload_fixup` exact transformations.
  c. The do_POST routing decision: `/v1/responses` + ChatGPT JWT selects the
     codex backend, while `/v1/responses` + sk- key does NOT (falls through).
"""

import json

from tokenpak.proxy.adapters.openai_codex_responses_adapter import (
    _is_chatgpt_oauth_token,
    codex_responses_payload_fixup,
)


# ---------------------------------------------------------------------------
# a. JWT vs sk- detection
# ---------------------------------------------------------------------------

def test_is_chatgpt_oauth_token_jwt_true():
    assert _is_chatgpt_oauth_token("Bearer eyJabc.def.ghi") is True


def test_is_chatgpt_oauth_token_sk_false():
    assert _is_chatgpt_oauth_token("Bearer sk-xxx") is False


def test_is_chatgpt_oauth_token_empty_false():
    assert _is_chatgpt_oauth_token("") is False
    assert _is_chatgpt_oauth_token("Bearer ") is False


# ---------------------------------------------------------------------------
# b. payload fixup
# ---------------------------------------------------------------------------

def test_payload_fixup_sets_stream_store_strips_max_and_converts_input():
    src = json.dumps({
        "model": "gpt-5.5",
        "stream": False,
        "store": True,
        "max_output_tokens": 1234,
        "input": "hello world",
    }).encode("utf-8")

    out = codex_responses_payload_fixup(src)
    parsed = json.loads(out)

    assert parsed["stream"] is True
    assert parsed["store"] is False
    assert "max_output_tokens" not in parsed
    assert parsed["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "hello world"}]}
    ]
    # No raw-content side channel beyond the documented input transform.
    assert parsed["model"] == "gpt-5.5"


def test_payload_fixup_empty_string_input_becomes_empty_list():
    src = json.dumps({"input": ""}).encode("utf-8")
    parsed = json.loads(codex_responses_payload_fixup(src))
    assert parsed["input"] == []


def test_payload_fixup_list_input_preserved():
    existing = [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
    src = json.dumps({"input": existing}).encode("utf-8")
    parsed = json.loads(codex_responses_payload_fixup(src))
    assert parsed["input"] == existing


def test_payload_fixup_returns_original_on_invalid_json():
    bad = b"not-json{"
    assert codex_responses_payload_fixup(bad) == bad


def test_payload_fixup_returns_original_on_non_dict():
    arr = b"[1, 2, 3]"
    assert codex_responses_payload_fixup(arr) == arr


# ---------------------------------------------------------------------------
# c. routing decision
# ---------------------------------------------------------------------------
#
# We exercise the exact branch logic from `_ProxyHandler._is_codex_v1_responses_request`
# without constructing a full BaseHTTPRequestHandler (which requires a live
# socket). The method depends only on `self.path` and `self.headers`, so a tiny
# stand-in that binds the real unbound method is a faithful unit.

from tokenpak.proxy.server import _ProxyHandler


class _FakeHeaders(dict):
    def get(self, key, default=None):  # case-sensitive like http.client.HTTPMessage
        return super().get(key, default)


class _FakeHandler:
    """Minimal stand-in exposing path + headers for the routing predicate."""

    def __init__(self, path, headers):
        self.path = path
        self.headers = headers

    # Bind the real predicate so we test production logic, not a copy.
    _is_codex_v1_responses_request = _ProxyHandler._is_codex_v1_responses_request


def _codex_backend_target(handler):
    """Return the upstream the do_POST branch would forward to, or None."""
    if handler._is_codex_v1_responses_request():
        return "https://chatgpt.com/backend-api/codex/responses"
    return None


def test_routing_jwt_selects_codex_backend():
    h = _FakeHandler(
        "/v1/responses",
        _FakeHeaders({"Authorization": "Bearer eyJabc.def.ghi"}),
    )
    assert _codex_backend_target(h) == "https://chatgpt.com/backend-api/codex/responses"


def test_routing_jwt_with_query_string_selects_codex_backend():
    h = _FakeHandler(
        "/v1/responses?foo=bar",
        _FakeHeaders({"Authorization": "Bearer eyJabc.def.ghi"}),
    )
    assert _codex_backend_target(h) == "https://chatgpt.com/backend-api/codex/responses"


def test_routing_sk_key_falls_through():
    h = _FakeHandler(
        "/v1/responses",
        _FakeHeaders({"Authorization": "Bearer sk-xxx"}),
    )
    assert _codex_backend_target(h) is None  # falls through to generic /v1/ branch


def test_routing_non_responses_path_falls_through():
    h = _FakeHandler(
        "/v1/chat/completions",
        _FakeHeaders({"Authorization": "Bearer eyJabc.def.ghi"}),
    )
    assert _codex_backend_target(h) is None
