"""ContextEnrichmentStage adapter-backed enrichment tests."""

from __future__ import annotations

import json

from tokenpak.core.routing.policy import Policy
from tokenpak.core.routing.route_class import RouteClass
from tokenpak.proxy.adapters import build_default_registry
from tokenpak.services.request import Request
from tokenpak.services.request_pipeline.stages import PipelineContext
from tokenpak.services.routing_service.context_enrichment import ContextEnrichmentStage


def _anthropic_body(user_msg: str, system: str | list | None = None) -> bytes:
    data = {"model": "claude-haiku-4-5", "messages": [{"role": "user", "content": user_msg}]}
    if system is not None:
        data["system"] = system
    return json.dumps(data).encode("utf-8")


def _openai_chat_body(user_msg: str, system: str | None = None) -> bytes:
    messages: list[dict[str, str]] = []
    if system is not None:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user_msg})
    return json.dumps({"model": "gpt-4o-mini", "messages": messages}).encode("utf-8")


def _openai_responses_body(user_msg: str, instructions: str | None = None) -> bytes:
    data = {"model": "gpt-4.1-mini", "input": user_msg}
    if instructions is not None:
        data["instructions"] = instructions
    return json.dumps(data).encode("utf-8")


def _ctx(body: bytes, policy: Policy, path: str) -> PipelineContext:
    ctx = PipelineContext(
        request=Request(
            body=body,
            headers={},
            metadata={"path": path},
        )
    )
    ctx.route_class = RouteClass.ANTHROPIC_SDK
    ctx.policy = policy
    return ctx


def _mock_retriever(hits: list[str]):
    def _search(query: str, top_k: int) -> list[str]:
        return hits[:top_k]

    return _search


def _stage(hits: list[str]) -> ContextEnrichmentStage:
    return ContextEnrichmentStage(
        retriever=_mock_retriever(hits),
        adapter_registry=build_default_registry(),
    )


def test_injection_disabled_is_noop():
    policy = Policy(injection_enabled=False, body_handling="mutate")
    body = _anthropic_body("a long prompt that should have triggered enrichment easily")
    ctx = _ctx(body, policy, "/v1/messages")
    _stage(["vault content"]).apply_request(ctx)
    assert ctx.request.body == body


def test_byte_preserve_skipped_with_telemetry():
    policy = Policy(injection_enabled=True, body_handling="byte_preserve")
    body = _anthropic_body("a long enough prompt that would normally trigger enrichment")
    ctx = _ctx(body, policy, "/v1/messages")
    _stage(["irrelevant"]).apply_request(ctx)
    # Body must not mutate on byte_preserve routes.
    assert ctx.request.body == body
    assert ctx.stage_telemetry["routing"]["enrichment_skipped"] == "byte_preserve"


def test_missing_capability_skips_passthrough_route():
    policy = Policy(
        injection_enabled=True,
        body_handling="mutate",
        injection_min_query_tokens=1,
    )
    body = json.dumps({"prompt": "tell me about the tokenpak adapter pattern"}).encode("utf-8")
    ctx = _ctx(body, policy, "/unknown")
    _stage(["vault content"]).apply_request(ctx)
    assert ctx.request.body == body
    assert ctx.stage_telemetry["routing"]["enrichment_skipped"] == "capability_missing"


def test_short_prompt_hits_relevance_gate():
    policy = Policy(injection_enabled=True, body_handling="mutate", injection_min_query_tokens=50)
    body = _anthropic_body("hi")
    ctx = _ctx(body, policy, "/v1/messages")
    _stage(["ctx"]).apply_request(ctx)
    assert ctx.request.body == body
    assert ctx.stage_telemetry["routing"]["enrichment_skipped"] == "below_min_query_tokens"


def test_long_anthropic_prompt_gets_enriched_through_adapter():
    policy = Policy(
        injection_enabled=True,
        body_handling="mutate",
        injection_budget_chars=500,
        injection_min_query_tokens=10,
    )
    long_prompt = (
        "please explain in detail how the proxy classifier resolves route classes end to end "
        * 3
    )
    ctx = _ctx(_anthropic_body(long_prompt), policy, "/v1/messages")
    _stage(["relevant vault snippet A", "relevant vault snippet B"]).apply_request(ctx)
    data = json.loads(ctx.request.body)
    system = data["system"]
    assert "tokenpak vault context" in ctx.request.body.decode("utf-8")
    if isinstance(system, str):
        assert system.startswith("[tokenpak vault context]")
    else:
        assert isinstance(system, list)
        assert system[0]["text"].startswith("[tokenpak vault context]")
    assert ctx.stage_telemetry["routing"]["enrichment_applied"] is True
    assert ctx.stage_telemetry["routing"]["format"] == "anthropic-messages"
    assert ctx.stage_telemetry["routing"]["injected_hits"] == 2


def test_openai_chat_completions_enriches_via_same_code_path():
    policy = Policy(
        injection_enabled=True,
        body_handling="mutate",
        injection_budget_chars=500,
        injection_min_query_tokens=5,
    )
    body = _openai_chat_body(
        "explain how tokenpak adapter capability gates should work for proxy cache",
        system="You are concise.",
    )
    ctx = _ctx(body, policy, "/v1/chat/completions")
    _stage(["chat vault snippet"]).apply_request(ctx)
    data = json.loads(ctx.request.body)
    assert data["messages"][0]["role"] == "system"
    assert data["messages"][0]["content"].startswith("[tokenpak vault context]")
    assert "chat vault snippet" in data["messages"][0]["content"]
    assert "You are concise." in data["messages"][0]["content"]
    assert data["messages"][1]["role"] == "user"
    assert ctx.stage_telemetry["routing"]["format"] == "openai-chat"


def test_openai_responses_enriches_instructions_via_same_code_path():
    policy = Policy(
        injection_enabled=True,
        body_handling="mutate",
        injection_budget_chars=500,
        injection_min_query_tokens=5,
    )
    body = _openai_responses_body(
        "summarize why adapter-normalized cache injection matters for codex requests",
        instructions="Answer in bullets.",
    )
    ctx = _ctx(body, policy, "/v1/responses")
    _stage(["responses vault snippet"]).apply_request(ctx)
    data = json.loads(ctx.request.body)
    assert data["instructions"].startswith("[tokenpak vault context]")
    assert "responses vault snippet" in data["instructions"]
    assert "Answer in bullets." in data["instructions"]
    assert data["input"] == (
        "summarize why adapter-normalized cache injection matters for codex requests"
    )
    assert ctx.stage_telemetry["routing"]["format"] == "openai-responses"


def test_injection_budget_truncates():
    policy = Policy(
        injection_enabled=True,
        body_handling="mutate",
        injection_budget_chars=100,  # small
        injection_min_query_tokens=10,
    )
    long_prompt = "explain the route classifier in detail please, it's important" * 4
    ctx = _ctx(_anthropic_body(long_prompt), policy, "/v1/messages")
    huge_hit = "x" * 10000
    _stage([huge_hit]).apply_request(ctx)
    # Injected chars should respect the budget (with a little overhead).
    injected = ctx.stage_telemetry["routing"]["injected_chars"]
    assert injected <= 100 + 50  # header + separator overhead


def test_no_retriever_no_change():
    """If the vault retriever can't be built, the Stage is a no-op."""
    policy = Policy(
        injection_enabled=True,
        body_handling="mutate",
        injection_min_query_tokens=10,
    )
    long_prompt = "please explain in detail how the classifier works " * 5
    body = _anthropic_body(long_prompt)
    ctx = _ctx(body, policy, "/v1/messages")
    class NoRetrieverStage(ContextEnrichmentStage):
        def _build_retriever(self):  # type: ignore[no-untyped-def]
            return None

    stage = NoRetrieverStage(adapter_registry=build_default_registry())
    stage.apply_request(ctx)
    assert ctx.request.body == body
    assert ctx.stage_telemetry["routing"]["enrichment_skipped"] == "no_retriever"


def test_injection_preserves_existing_system_string():
    policy = Policy(
        injection_enabled=True,
        body_handling="mutate",
        injection_budget_chars=1000,
        injection_min_query_tokens=10,
    )
    body = _anthropic_body(
        "please describe the architecture in detail for our integration work",
        system="You are a helpful assistant.",
    )
    ctx = _ctx(body, policy, "/v1/messages")
    _stage(["vault snippet"]).apply_request(ctx)
    data = json.loads(ctx.request.body)
    assert isinstance(data["system"], str)
    assert data["system"].startswith("[tokenpak vault context]")
    assert "vault snippet" in data["system"]
    assert "You are a helpful assistant." in data["system"]


def test_anthropic_specific_helpers_removed_from_stage():
    assert not hasattr(ContextEnrichmentStage, "_extract_query_text")
    assert not hasattr(ContextEnrichmentStage, "_inject_into_system")
