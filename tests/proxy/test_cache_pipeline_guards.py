# SPDX-License-Identifier: Apache-2.0
"""Regression tests for provider cache-hint injection and stripping."""

from __future__ import annotations

import json

from tokenpak.core.runtime.providers import Provider
from tokenpak.proxy.cache_pipeline import (
    CacheMode,
    _apply_anthropic_auto_cache,
    _inject_bedrock_checkpoints,
    _inject_gemini_cache_ref,
    _inject_prompt_cache_key,
    _select_anthropic_cache_mode,
)


def _body(**fields) -> bytes:
    return json.dumps(fields).encode("utf-8")


def _decode(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))


def test_prompt_cache_key_injected_only_for_supported_providers():
    supported = (Provider.OPENAI, Provider.AZURE_OPENAI, Provider.CODEX, Provider.XAI)
    unsupported = (
        Provider.ANTHROPIC,
        Provider.GEMINI,
        Provider.BEDROCK,
        Provider.UNKNOWN,
    )
    headers = {"x-tokenpak-cache-key": "stable-session"}

    for provider in supported:
        out = _decode(
            _inject_prompt_cache_key(
                provider,
                headers,
                _body(model="m", input="hi", tokenpak_cache_hint="body-key"),
            )
        )
        assert out["prompt_cache_key"] == "stable-session"
        assert "tokenpak_cache_hint" not in out

    for provider in unsupported:
        out = _decode(
            _inject_prompt_cache_key(
                provider,
                headers,
                _body(model="m", input="hi", tokenpak_cache_hint="body-key"),
            )
        )
        assert "prompt_cache_key" not in out
        assert "tokenpak_cache_hint" not in out


def test_prompt_cache_retention_is_stripped_and_only_forwarded_when_supported():
    body = _body(
        model="m",
        input="hi",
        tokenpak_cache_hint="body-key",
        tokenpak_cache_retention="5m",
    )

    supported = _decode(_inject_prompt_cache_key(Provider.OPENAI, {}, body))
    assert supported["prompt_cache_key"] == "body-key"
    assert supported["prompt_cache_retention"] == "5m"
    assert "tokenpak_cache_hint" not in supported
    assert "tokenpak_cache_retention" not in supported

    unsupported = _decode(_inject_prompt_cache_key(Provider.GEMINI, {}, body))
    assert "prompt_cache_key" not in unsupported
    assert "prompt_cache_retention" not in unsupported
    assert "tokenpak_cache_hint" not in unsupported
    assert "tokenpak_cache_retention" not in unsupported


def test_gemini_cache_ref_injects_cached_content_and_strips_body_hint():
    out = _decode(
        _inject_gemini_cache_ref(
            Provider.GEMINI,
            {},
            _body(contents=[], tokenpak_cache_object_ref="cachedContents/abc"),
        )
    )

    assert out["cachedContent"] == "cachedContents/abc"
    assert "tokenpak_cache_object_ref" not in out


def test_gemini_header_ref_takes_precedence_over_body_hint():
    out = _decode(
        _inject_gemini_cache_ref(
            Provider.GEMINI,
            {"x-tokenpak-cache-ref": "cachedContents/header"},
            _body(contents=[], tokenpak_cache_object_ref="cachedContents/body"),
        )
    )

    assert out["cachedContent"] == "cachedContents/header"
    assert "tokenpak_cache_object_ref" not in out


def test_bedrock_checkpoints_inject_cache_points_and_strip_hint():
    out = _decode(
        _inject_bedrock_checkpoints(
            Provider.BEDROCK,
            _body(
                messages=[
                    {"role": "user", "content": "one"},
                    {"role": "assistant", "content": "two"},
                ],
                tokenpak_checkpoints=[0, 99, "bad", 0],
            ),
        )
    )

    assert "tokenpak_checkpoints" not in out
    assert out["messages"] == [
        {"role": "user", "content": "one"},
        {"cachePoint": {"type": "default"}},
        {"role": "assistant", "content": "two"},
    ]


def test_anthropic_cache_mode_hint_is_stripped_and_applied():
    body = {"messages": [{"role": "user", "content": "hi"}], "tokenpak_cache_mode": "auto"}

    mode = _select_anthropic_cache_mode({}, body)

    assert mode is CacheMode.AUTO
    assert "tokenpak_cache_mode" not in body


def test_anthropic_auto_cache_removes_block_markers_before_top_level_cache():
    body = {
        "system": [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}],
        "tools": [{"name": "tool", "cache_control": {"type": "ephemeral"}}],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "hi",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ],
    }

    _apply_anthropic_auto_cache(body)

    assert body["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in body["system"][0]
    assert "cache_control" not in body["tools"][0]
    assert "cache_control" not in body["messages"][0]["content"][0]
