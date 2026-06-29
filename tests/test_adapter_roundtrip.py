"""Round-trip smoke tests using real-format adapter request/response bodies.

All tests are offline — no live API calls, no credentials required.
Covers every adapter in the proxy registry and the telemetry registry.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tokenpak.adapters.anthropic", reason="module not available in current build")
import json
from pathlib import Path

from tokenpak.adapters.anthropic import AnthropicAdapter
from tokenpak.adapters.openai import OpenAIAdapter

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# SDK adapter round-trips (existing — unchanged)
# ---------------------------------------------------------------------------


class TestAnthropicAdapterRoundtrip:
    def test_normalize_real_request(self):
        request = _load("anthropic_messages_request.json")
        adapter = AnthropicAdapter(base_url="http://localhost:8767", api_key="sk-test")

        normalized = adapter.prepare_request(request)

        assert normalized["model"] == request["model"]
        assert normalized["max_tokens"] == request["max_tokens"]
        assert normalized["messages"] == request["messages"]
        assert normalized["system"] == request["system"]
        assert normalized["stream"] is False

    def test_denormalize_roundtrip(self):
        request = _load("anthropic_messages_request.json")
        adapter = AnthropicAdapter(base_url="http://localhost:8767", api_key="sk-test")

        denormalized = adapter.prepare_request(request)

        expected = dict(request)
        expected["stream"] = False
        assert denormalized == expected


class TestOpenAIChatAdapterRoundtrip:
    def test_normalize_real_request(self):
        request = _load("openai_chat_request.json")
        adapter = OpenAIAdapter(base_url="http://localhost:8767", api_key="sk-test")

        normalized = adapter.prepare_request(request)

        assert normalized["model"] == request["model"]
        assert normalized["messages"] == request["messages"]
        assert normalized["tools"] == request["tools"]
        assert normalized["stream"] is False

    def test_denormalize_roundtrip(self):
        request = _load("openai_chat_request.json")
        adapter = OpenAIAdapter(base_url="http://localhost:8767", api_key="sk-test")

        denormalized = adapter.prepare_request(request)

        expected = dict(request)
        expected["stream"] = False
        assert denormalized == expected


# ---------------------------------------------------------------------------
# Proxy adapter round-trips — capability smoke checks
# ---------------------------------------------------------------------------


def _b(payload: dict) -> bytes:
    return json.dumps(payload).encode()


class TestProxyAnthropicRoundtrip:
    """Anthropic proxy adapter: token counting, streaming, tools, cache injection."""

    def setup_method(self):
        from tokenpak.proxy.adapters.anthropic_adapter import AnthropicAdapter
        self.adapter = AnthropicAdapter()

    def test_roundtrip_preserves_all_fields(self):
        body = _b({
            "model": "claude-sonnet-4-6",
            "system": "Be concise.",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 64,
            "stream": False,
        })
        canonical = self.adapter.normalize(body)
        restored = json.loads(self.adapter.denormalize(canonical))
        assert restored["model"] == "claude-sonnet-4-6"
        assert restored["max_tokens"] == 64
        assert restored["stream"] is False

    def test_extract_output_tokens_from_response(self):
        resp = _b({"usage": {"input_tokens": 20, "output_tokens": 8}})
        assert self.adapter.extract_response_tokens(resp) == 8

    def test_extract_input_tokens_via_base_class(self):
        # FormatAdapter.extract_request_tokens uses normalize()
        body = _b({
            "model": "claude-sonnet-4-6",
            "system": "Be concise.",
            "messages": [{"role": "user", "content": "What is 2+2?"}],
        })
        model, tokens = self.adapter.extract_request_tokens(body)
        assert model == "claude-sonnet-4-6"
        assert tokens > 0

    def test_tools_roundtrip(self):
        body = _b({
            "model": "claude-sonnet-4-6",
            "messages": [{"role": "user", "content": "Call my_tool"}],
            "tools": [{"name": "my_tool", "description": "Does something", "input_schema": {"type": "object", "properties": {}}}],
        })
        canonical = self.adapter.normalize(body)
        assert canonical.tools is not None and len(canonical.tools) == 1
        restored = json.loads(self.adapter.denormalize(canonical))
        assert restored["tools"][0]["name"] == "my_tool"

    def test_cacheable_injection_stable_no_volatile_cache_control(self):
        body = _b({
            "model": "claude-sonnet-4-6",
            "system": "You are helpful.",
            "messages": [{"role": "user", "content": "hi"}],
        })
        updated = json.loads(self.adapter.inject_system_context(body, "recall: fact A"))
        system = updated["system"]
        assert isinstance(system, list)
        # Stable prefix gets cache_control; volatile does not
        assert system[0].get("cache_control") == {"type": "ephemeral"}
        assert "cache_control" not in system[1]

    def test_two_layer_cacheable_injection(self):
        body = _b({
            "model": "claude-sonnet-4-6",
            "system": "Base.",
            "messages": [{"role": "user", "content": "hi"}],
        })
        trace: dict = {}
        updated = json.loads(
            self.adapter.inject_system_context(
                body,
                stable_text="stable part",
                volatile_text="volatile part",
                trace_out=trace,
            )
        )
        system = updated["system"]
        texts = [b["text"] for b in system]
        assert "stable part" in texts
        assert "volatile part" in texts
        assert trace["cache_origin"] == "proxy"
        # volatile block (last) must not have cache_control
        assert "cache_control" not in system[-1]

    def test_sse_format_is_anthropic(self):
        assert self.adapter.get_sse_format() == "anthropic-sse"

    def test_default_upstream(self):
        assert "anthropic.com" in self.adapter.get_default_upstream()


class TestProxyOpenAIChatRoundtrip:
    """OpenAI Chat proxy adapter: system extraction, tools, token extraction."""

    def setup_method(self):
        from tokenpak.proxy.adapters.openai_chat_adapter import OpenAIChatAdapter
        self.adapter = OpenAIChatAdapter()

    def test_system_message_extracted_to_canonical(self):
        body = _b({
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "Follow policy."},
                {"role": "user", "content": "Hello"},
            ],
        })
        canonical = self.adapter.normalize(body)
        assert canonical.system == "Follow policy."
        assert canonical.messages[0]["role"] == "user"

    def test_system_message_restored_on_denormalize(self):
        body = _b({
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "Go"},
            ],
        })
        canonical = self.adapter.normalize(body)
        restored = json.loads(self.adapter.denormalize(canonical))
        assert restored["messages"][0]["role"] == "system"
        assert restored["messages"][0]["content"] == "Be brief."

    def test_legacy_functions_promoted_to_tools(self):
        body = _b({
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "call it"}],
            "functions": [{"name": "do_thing", "parameters": {}}],
        })
        canonical = self.adapter.normalize(body)
        assert canonical.tools is not None

    def test_extract_output_tokens(self):
        resp = _b({"usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        assert self.adapter.extract_response_tokens(resp) == 5

    def test_extract_input_tokens(self):
        resp = _b({"usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        assert self.adapter.extract_input_tokens(resp) == 10

    def test_sse_format(self):
        assert self.adapter.get_sse_format() == "openai-sse"


class TestProxyOpenAIResponsesRoundtrip:
    """OpenAI Responses API proxy adapter: input format round-trips, cache key."""

    def setup_method(self):
        from tokenpak.proxy.adapters.openai_responses_adapter import OpenAIResponsesAdapter
        self.adapter = OpenAIResponsesAdapter()

    def test_string_input_roundtrip(self):
        body = _b({
            "model": "gpt-5.1-mini",
            "instructions": "Be helpful.",
            "input": "What is 3+3?",
            "stream": False,
        })
        canonical = self.adapter.normalize(body)
        assert canonical.system == "Be helpful."
        restored = json.loads(self.adapter.denormalize(canonical))
        assert isinstance(restored["input"], str)
        assert restored["input"] == "What is 3+3?"

    def test_message_array_input_roundtrip(self):
        body = _b({
            "model": "gpt-5.1-mini",
            "instructions": "Be concise.",
            "input": [
                {"role": "user", "content": "First turn"},
                {"role": "assistant", "content": "OK"},
                {"role": "user", "content": "Second turn"},
            ],
        })
        canonical = self.adapter.normalize(body)
        assert canonical.messages[-1]["content"] == "Second turn"
        restored = json.loads(self.adapter.denormalize(canonical))
        assert isinstance(restored["input"], list)
        assert restored["input"][0]["role"] == "user"

    def test_tools_sorted_deterministically(self):
        body = _b({
            "model": "gpt-5.1-mini",
            "input": "hi",
            "tools": [
                {"type": "function", "function": {"name": "z_tool"}},
                {"type": "function", "function": {"name": "a_tool"}},
            ],
        })
        out1 = json.loads(self.adapter.denormalize(self.adapter.normalize(body)))
        out2 = json.loads(self.adapter.denormalize(self.adapter.normalize(body)))
        assert out1["tools"] == out2["tools"]

    def test_prompt_cache_key_stable_across_identical_requests(self):
        body = _b({
            "model": "gpt-5.1-mini",
            "instructions": "Stable instruction.",
            "input": [{"role": "user", "content": "query"}],
        })
        out1 = json.loads(self.adapter.denormalize(self.adapter.normalize(body)))
        out2 = json.loads(self.adapter.denormalize(self.adapter.normalize(body)))
        assert out1["prompt_cache_key"] == out2["prompt_cache_key"]

    def test_capability_label_semantic_cache(self):
        assert "tip.cache.semantic.v1" in self.adapter.capabilities

    def test_sse_format(self):
        assert self.adapter.get_sse_format() == "openai-responses-sse"


class TestProxyOpenAICodexRoundtrip:
    """OpenAI Codex proxy adapter: detection, stream forced, store stripped."""

    def setup_method(self):
        from tokenpak.proxy.adapters.openai_codex_responses_adapter import (
            OpenAICodexResponsesAdapter,
            codex_responses_payload_fixup,
        )
        self.adapter = OpenAICodexResponsesAdapter()
        self.fixup = codex_responses_payload_fixup

    def test_detect_codex_path(self):
        assert self.adapter.detect("/codex/responses", {}, None)
        assert self.adapter.detect("/v1/codex/responses", {}, None)

    def test_detect_jwt_on_v1_responses(self):
        # eyJ prefix = JWT
        jwt = "eyJhbGciOiJSUzI1NiJ9.payload.sig"
        assert self.adapter.detect("/v1/responses", {"Authorization": f"Bearer {jwt}"}, None)

    def test_detect_rejects_sk_api_key_on_v1_responses(self):
        # sk- prefix = OpenAI API key, not Codex
        assert not self.adapter.detect("/v1/responses", {"Authorization": "Bearer sk-abc123"}, None)

    def test_denormalize_forces_stream_true_store_false(self):
        body = _b({
            "model": "codex-mini-latest",
            "instructions": "Help.",
            "input": "Fix this bug",
            "stream": False,
            "store": True,
            "max_output_tokens": 100,
        })
        canonical = self.adapter.normalize(body)
        restored = json.loads(self.adapter.denormalize(canonical))
        assert restored["stream"] is True
        assert restored["store"] is False
        assert "max_output_tokens" not in restored

    def test_fixup_string_input_to_list(self):
        body = _b({"model": "codex-mini-latest", "input": "Hello", "stream": False})
        fixed = json.loads(self.fixup(body))
        assert isinstance(fixed["input"], list)
        assert fixed["input"][0]["role"] == "user"

    def test_fixup_preserves_list_input(self):
        msgs = [{"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}]
        body = _b({"model": "codex-mini-latest", "input": msgs})
        fixed = json.loads(self.fixup(body))
        assert fixed["input"] == msgs

    def test_default_upstream_is_chatgpt(self):
        assert "chatgpt.com" in self.adapter.get_default_upstream()


class TestProxyGrokRoundtrip:
    """xAI Grok proxy adapter: detection, tools, cost model, OpenAI-compatible wire."""

    def setup_method(self):
        from tokenpak.proxy.adapters.grok_adapter import GrokAdapter
        self.adapter = GrokAdapter()

    def test_detect_x_ai_host(self):
        assert self.adapter.detect("/v1/chat/completions", {"host": "api.x.ai"}, None)

    def test_detect_xai_header(self):
        assert self.adapter.detect("/anything", {"x-xai-api-key": "secret"}, None)

    def test_detect_grok_model_in_body(self):
        body = _b({"model": "grok-3-mini", "messages": []})
        assert self.adapter.detect("/v1/chat/completions", {}, body)

    def test_does_not_detect_non_grok_body(self):
        body = _b({"model": "gpt-4o", "messages": []})
        assert not self.adapter.detect("/v1/chat/completions", {}, body)

    def test_roundtrip_preserves_system_message(self):
        body = _b({
            "model": "grok-3-mini",
            "messages": [
                {"role": "system", "content": "You are Grok."},
                {"role": "user", "content": "Hello"},
            ],
        })
        canonical = self.adapter.normalize(body)
        assert canonical.system == "You are Grok."
        restored = json.loads(self.adapter.denormalize(canonical))
        assert restored["messages"][0]["role"] == "system"

    def test_tools_roundtrip(self):
        body = _b({
            "model": "grok-3",
            "messages": [{"role": "user", "content": "use it"}],
            "tools": [{"type": "function", "function": {"name": "my_fn", "parameters": {}}}],
        })
        canonical = self.adapter.normalize(body)
        assert canonical.tools is not None
        restored = json.loads(self.adapter.denormalize(canonical))
        assert restored["tools"][0]["function"]["name"] == "my_fn"

    def test_extract_output_tokens(self):
        resp = _b({"usage": {"completion_tokens": 42}})
        assert self.adapter.extract_response_tokens(resp) == 42

    def test_cost_model_known_models(self):
        cost = self.adapter.estimate_cost("grok-3-mini", input_tokens=1_000_000, output_tokens=1_000_000)
        assert cost["input_cost"] == pytest.approx(0.30)
        assert cost["output_cost"] == pytest.approx(0.50)
        assert cost["total_cost"] == pytest.approx(0.80)

    def test_cost_model_unknown_model_uses_default(self):
        cost = self.adapter.estimate_cost("grok-unknown-future", input_tokens=0, output_tokens=0)
        assert cost["total_cost"] == pytest.approx(0.0)

    def test_sse_format_is_openai(self):
        assert self.adapter.get_sse_format() == "openai-sse"

    def test_default_upstream(self):
        assert "x.ai" in self.adapter.get_default_upstream()


class TestProxyGoogleRoundtrip:
    """Google Generative AI proxy adapter: contents/systemInstruction, tool translation."""

    def setup_method(self):
        from tokenpak.proxy.adapters.google_adapter import GoogleGenerativeAIAdapter
        self.adapter = GoogleGenerativeAIAdapter()

    def test_detect_v1beta_path(self):
        assert self.adapter.detect("/v1beta/models/gemini-2-flash:generateContent", {}, None)

    def test_detect_goog_api_key(self):
        assert self.adapter.detect("/anything", {"x-goog-api-key": "AIzaXXX"}, None)

    def test_roundtrip_system_instruction_dict(self):
        body = _b({
            "model": "gemini-2-flash",
            "systemInstruction": {"parts": [{"text": "Be precise."}]},
            "contents": [{"role": "user", "parts": [{"text": "Hi"}]}],
        })
        canonical = self.adapter.normalize(body)
        assert canonical.messages[0]["role"] == "user"
        restored = json.loads(self.adapter.denormalize(canonical))
        assert restored["systemInstruction"]["parts"][0]["text"] == "Be precise."

    def test_roundtrip_system_instruction_string(self):
        body = _b({
            "model": "gemini-2-flash",
            "systemInstruction": "Be a helpful assistant.",
            "contents": [{"role": "user", "parts": [{"text": "Hi"}]}],
        })
        canonical = self.adapter.normalize(body)
        restored = json.loads(self.adapter.denormalize(canonical))
        # String system gets stored as a string in canonical; restored via _to_google_parts
        assert restored["systemInstruction"]["parts"][0]["text"] == "Be a helpful assistant."

    def test_tools_translated_from_openai_format(self):
        body = _b({
            "model": "gemini-2-flash",
            "contents": [{"role": "user", "parts": [{"text": "call it"}]}],
            "tools": [{"type": "function", "function": {"name": "my_fn", "description": "does X", "parameters": {"type": "object", "properties": {}}}}],
        })
        restored = json.loads(self.adapter.denormalize(self.adapter.normalize(body)))
        decls = restored["tools"][0]["functionDeclarations"]
        assert decls[0]["name"] == "my_fn"

    def test_tools_translated_from_anthropic_format(self):
        body = _b({
            "model": "gemini-2-flash",
            "contents": [{"role": "user", "parts": [{"text": "go"}]}],
            "tools": [{"name": "ant_tool", "description": "does Y", "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}}}],
        })
        restored = json.loads(self.adapter.denormalize(self.adapter.normalize(body)))
        decls = restored["tools"][0]["functionDeclarations"]
        assert decls[0]["name"] == "ant_tool"
        # type should be uppercased
        assert decls[0]["parameters"]["properties"]["x"]["type"] == "STRING"

    def test_schema_sanitization_removes_unsupported_keys(self):
        body = _b({
            "model": "gemini-2-flash",
            "contents": [{"role": "user", "parts": [{"text": "go"}]}],
            "tools": [{"name": "t", "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "$schema": "http://json-schema.org/draft-07/schema#",
                "properties": {"val": {"type": "string", "default": "x", "title": "Val"}},
            }}],
        })
        restored = json.loads(self.adapter.denormalize(self.adapter.normalize(body)))
        decls = restored["tools"][0]["functionDeclarations"]
        params = decls[0].get("parameters", {})
        assert "additionalProperties" not in params
        assert "$schema" not in params
        # default and title are also stripped
        assert "default" not in params.get("properties", {}).get("val", {})
        assert "title" not in params.get("properties", {}).get("val", {})

    def test_extract_output_tokens(self):
        resp = _b({"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}})
        assert self.adapter.extract_response_tokens(resp) == 5

    def test_extract_input_tokens(self):
        resp = _b({"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}})
        assert self.adapter.extract_input_tokens(resp) == 10

    def test_streaming_detected_by_url(self):
        assert self.adapter.detect_streaming("/v1beta/models/gemini:streamGenerateContent")
        assert self.adapter.detect_streaming("/path?alt=sse")
        assert not self.adapter.detect_streaming("/v1beta/models/gemini:generateContent")

    def test_sse_format_is_google_ndjson(self):
        assert self.adapter.get_sse_format() == "google-ndjson"


class TestProxyPassthroughRoundtrip:
    """Passthrough adapter: byte preservation, inject noop, no token extraction."""

    def setup_method(self):
        from tokenpak.proxy.adapters.passthrough_adapter import PassthroughAdapter
        self.adapter = PassthroughAdapter()

    def test_roundtrip_identity(self):
        body = _b({"model": "custom", "prompt": "go", "custom": {"nested": True}})
        canonical = self.adapter.normalize(body)
        restored = json.loads(self.adapter.denormalize(canonical))
        assert restored["custom"]["nested"] is True

    def test_inject_is_noop(self):
        body = _b({"model": "custom", "prompt": "hi"})
        assert self.adapter.inject_system_context(body, "ignored") == body

    def test_inject_two_layer_is_noop(self):
        body = _b({"model": "custom", "prompt": "hi"})
        trace: dict = {}
        result = self.adapter.inject_system_context(
            body, stable_text="S", volatile_text="V", trace_out=trace
        )
        assert result == body
        assert trace["cache_origin"] == "client"
        assert trace["stable_present"] is False

    def test_output_tokens_returns_zero_no_crash(self):
        body = _b({"model": "custom", "result": "done"})
        assert self.adapter.extract_response_tokens(body) == 0


# ---------------------------------------------------------------------------
# Telemetry adapter round-trips
# ---------------------------------------------------------------------------


class TestTelemetryAnthropicAdapter:
    """Anthropic telemetry adapter: detection, usage extraction, cache fields."""

    def setup_method(self):
        from tokenpak.telemetry.adapters.anthropic import AnthropicAdapter
        self.adapter = AnthropicAdapter()

    def test_detect_response_high_confidence(self):
        raw = {
            "type": "message",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "hi"}],
        }
        _name, score = self.adapter.detect(raw)
        assert score >= 0.9

    def test_detect_request_with_anthropic_version(self):
        raw = {"anthropic-version": "2023-06-01", "model": "claude-3-5-sonnet-20241022"}
        _name, score = self.adapter.detect(raw)
        assert score >= 0.7

    def test_detect_openai_response_returns_zero(self):
        raw = {"choices": [{"message": {"content": "hi"}}], "usage": {}}
        _name, score = self.adapter.detect(raw)
        assert score == 0.0

    def test_extract_usage_full(self):
        raw = {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 5,
            }
        }
        usage = self.adapter.extract_usage(raw)
        assert usage.input_billed == 100
        assert usage.output_billed == 50
        assert usage.cache_read == 20
        assert usage.cache_write == 5
        assert usage.usage_source == "provider_reported"
        assert usage.confidence == "high"

    def test_extract_usage_empty_returns_low_confidence(self):
        usage = self.adapter.extract_usage({})
        assert usage.confidence == "low"
        assert usage.usage_source == "unknown"

    def test_to_canonical_request_system_injected(self):
        raw = {
            "model": "claude-3-5-sonnet-20241022",
            "system": "Be concise.",
            "messages": [{"role": "user", "content": "Hi"}],
        }
        req = self.adapter.to_canonical_request(raw)
        assert req.messages[0]["role"] == "system"
        assert req.messages[0]["content"] == "Be concise."
        assert req.messages[1]["role"] == "user"

    def test_to_canonical_response_tool_use_stop_reason(self):
        raw = {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "x", "name": "fn", "input": {}}],
        }
        resp = self.adapter.to_canonical_response(raw)
        assert resp.finish_reason == "tool_use"


class TestTelemetryOpenAIAdapter:
    """OpenAI telemetry adapter: detection, usage, codex, Responses API."""

    def setup_method(self):
        from tokenpak.telemetry.adapters.openai import OpenAIAdapter
        self.adapter = OpenAIAdapter()

    def test_detect_chat_completion_high_confidence(self):
        raw = {"object": "chat.completion", "choices": []}
        _name, score = self.adapter.detect(raw)
        assert score == 1.0

    def test_detect_responses_api(self):
        raw = {"object": "response", "output": []}
        _name, score = self.adapter.detect(raw)
        assert score == 1.0

    def test_detect_anthropic_returns_zero(self):
        raw = {"stop_reason": "end_turn", "content": []}
        _name, score = self.adapter.detect(raw)
        assert score == 0.0

    def test_extract_usage_chat(self):
        raw = {
            "object": "chat.completion",
            "choices": [],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 5},
            },
        }
        usage = self.adapter.extract_usage(raw)
        assert usage.input_billed == 20
        assert usage.output_billed == 10
        assert usage.cache_read == 5
        assert usage.cache_write == 0  # OpenAI does not expose cache-write
        assert usage.usage_source == "provider_reported"
        assert usage.confidence == "high"

    def test_extract_usage_missing_returns_low(self):
        usage = self.adapter.extract_usage({"object": "chat.completion"})
        assert usage.confidence == "low"

    def test_to_canonical_request_promotes_functions(self):
        raw = {
            "model": "gpt-4o",
            "messages": [],
            "functions": [{"name": "fn_a", "parameters": {}}],
        }
        req = self.adapter.to_canonical_request(raw)
        assert any(t.get("function", {}).get("name") == "fn_a" for t in req.tools)

    def test_to_canonical_response_finish_reason_mapped(self):
        raw = {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "tool_calls"}]
        }
        resp = self.adapter.to_canonical_response(raw)
        assert resp.finish_reason == "tool_use"


class TestTelemetryGeminiAdapter:
    """Gemini telemetry adapter: detection, usage, graceful degradation."""

    def setup_method(self):
        from tokenpak.telemetry.adapters.gemini import GeminiAdapter
        self.adapter = GeminiAdapter()

    def test_detect_response_high_confidence(self):
        raw = {"candidates": [], "usageMetadata": {}}
        _name, score = self.adapter.detect(raw)
        assert score == 1.0

    def test_detect_request_medium_confidence(self):
        raw = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
        _name, score = self.adapter.detect(raw)
        assert score >= 0.6

    def test_detect_openai_response_returns_zero(self):
        raw = {"choices": [], "usage": {}}
        _name, score = self.adapter.detect(raw)
        assert score == 0.0

    def test_extract_usage_full(self):
        raw = {
            "usageMetadata": {
                "promptTokenCount": 80,
                "candidatesTokenCount": 30,
                "cachedContentTokenCount": 10,
            }
        }
        usage = self.adapter.extract_usage(raw)
        assert usage.input_billed == 80
        assert usage.output_billed == 30
        assert usage.cache_read == 10
        assert usage.cache_write == 0  # Gemini does not expose cache-write
        assert usage.usage_source == "provider_reported"
        assert usage.confidence == "high"

    def test_extract_usage_missing_metadata_degrades_gracefully(self):
        # Simulates a streaming chunk with no usageMetadata
        usage = self.adapter.extract_usage({"candidates": [{"content": {"parts": [{"text": "partial"}]}}]})
        assert usage.confidence == "low"
        assert usage.usage_source == "unknown"

    def test_to_canonical_response_text_output(self):
        raw = {
            "candidates": [{"content": {"parts": [{"text": "Hello"}]}, "finishReason": "STOP"}]
        }
        resp = self.adapter.to_canonical_response(raw)
        assert resp.finish_reason == "stop"
        assert "Hello" in resp.output

    def test_to_canonical_response_no_candidates_returns_error(self):
        resp = self.adapter.to_canonical_response({"candidates": []})
        assert resp.error is not None


# ---------------------------------------------------------------------------
# Telemetry registry detection order
# ---------------------------------------------------------------------------


class TestTelemetryRegistryDetection:
    """Registry picks the highest-confidence adapter; falls back to UnknownAdapter."""

    def setup_method(self):
        from tokenpak.telemetry.adapters.registry import AdapterRegistry
        self.registry = AdapterRegistry.build_default()

    def test_anthropic_response_detected(self):
        raw = {
            "type": "message",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }
        adapter = self.registry.detect(raw)
        assert adapter.provider_name == "anthropic"

    def test_openai_chat_response_detected(self):
        raw = {
            "object": "chat.completion",
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }
        adapter = self.registry.detect(raw)
        assert adapter.provider_name == "openai"

    def test_gemini_response_detected(self):
        raw = {
            "candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3},
        }
        adapter = self.registry.detect(raw)
        assert adapter.provider_name == "gemini"

    def test_unknown_payload_falls_back_to_unknown_adapter(self):
        from tokenpak.telemetry.adapters.registry import UnknownAdapter
        raw = {"some_field": "some_value"}
        adapter = self.registry.detect(raw)
        assert isinstance(adapter, UnknownAdapter)

    def test_unknown_adapter_returns_proxy_estimate(self):
        from tokenpak.telemetry.adapters.registry import UnknownAdapter
        usage = UnknownAdapter().extract_usage({"some": "data"})
        assert usage.usage_source == "proxy_estimate"
        assert usage.confidence == "low"
