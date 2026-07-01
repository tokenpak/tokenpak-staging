# SPDX-License-Identifier: Apache-2.0
"""Static conformance coverage for completion-path request-ledger writes."""

from __future__ import annotations

from pathlib import Path

PROVIDER_MODEL_PAIRS = (
    ("anthropic", "claude-opus-4-1"),
    ("anthropic", "claude-sonnet-4-6"),
    ("anthropic", "claude-haiku-4-5"),
    ("openai-codex", "gpt-5-codex"),
)


def _source(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_provider_model_matrix_covers_required_completion_families() -> None:
    assert ("anthropic", "claude-opus-4-1") in PROVIDER_MODEL_PAIRS
    assert ("anthropic", "claude-sonnet-4-6") in PROVIDER_MODEL_PAIRS
    assert ("anthropic", "claude-haiku-4-5") in PROVIDER_MODEL_PAIRS
    assert ("openai-codex", "gpt-5-codex") in PROVIDER_MODEL_PAIRS


def test_monitor_schema_uses_empty_attribution_sentinel() -> None:
    monitor_source = _source("tokenpak/proxy/monitor.py")

    assert "agent_id TEXT DEFAULT ''" in monitor_source
    assert "cycle_id TEXT DEFAULT ''" in monitor_source
    assert "attribution_source TEXT DEFAULT 'unknown'" in monitor_source
    assert "agent_id or \"\"" in monitor_source
    assert "cycle_id or \"\"" in monitor_source
    assert 'attribution_source or "unknown"' in monitor_source


def test_header_resolvers_use_empty_attribution_sentinel() -> None:
    pipeline_source = _source("tokenpak/proxy/request_pipeline.py")

    agent_block = pipeline_source[
        pipeline_source.index("def _resolve_agent_id")
        : pipeline_source.index("def _resolve_cycle_id")
    ]
    cycle_block = pipeline_source[
        pipeline_source.index("def _resolve_cycle_id")
        : pipeline_source.index("# ---------------------------------------------------------------------------", pipeline_source.index("def _resolve_cycle_id"))
    ]

    assert 'return ""' in agent_block
    assert 'return ""' in cycle_block


def test_proxy_completion_log_call_sites_thread_attribution_fields() -> None:
    server_source = _source("tokenpak/proxy/server.py")
    async_source = _source("tokenpak/proxy/server_async.py")
    codex_adapter_source = _source(
        "tokenpak/proxy/adapters/openai_codex_responses_adapter.py"
    )

    anthropic_block = server_source[
        server_source.index("ps.monitor.log(") : server_source.index(
            "# Record cache telemetry"
        )
    ]
    assert "session_id=_mon_session_id" in anthropic_block
    assert "agent_id=_mon_agent_id" in anthropic_block
    assert "cycle_id=_mon_cycle_id" in anthropic_block

    codex_block = server_source[
        server_source.index("# ── Codex /v1/responses monitor row")
        : server_source.index("# ── Circuit breaker: record outcome")
    ]
    assert "_cx_cache_read" in codex_block
    assert 'cache_origin="upstream"' in codex_block
    assert "session_id=_cx_session_id" in codex_block
    assert "agent_id=_cx_agent_id" in codex_block
    assert "cycle_id=_cx_cycle_id" in codex_block
    assert "attribution_source=_cx_attribution_source" in codex_block

    async_block = async_source[
        async_source.index("def _record_telemetry")
        : async_source.index("# ---------------------------------------------------------------------------", async_source.index("def _record_telemetry"))
    ]
    assert "def _resolve_async_monitor_attribution" in async_source
    assert "monitor.log(" in async_block
    assert "session_id=session_id" in async_block
    assert "agent_id=agent_id" in async_block
    assert "cycle_id=cycle_id" in async_block

    assert "def _extract_codex_responses_usage_from_sse_tail" in codex_adapter_source
