# SPDX-License-Identifier: Apache-2.0
"""Both /health emitters must share one canonical CLI-facing contract.

Guards the tester-readiness schema-drift defect: the threaded emitter
(``build_health_response``) reported ``compilation_mode`` / ``stats.requests`` /
``latency`` but no ``version`` / ``uptime_seconds``, while the async emitter
(``ProxyServer.health()``) reported ``version`` / ``uptime_seconds`` /
``requests_total`` but no ``compilation_mode`` / ``stats`` / ``latency``. A CLI
reader (``tokenpak doctor`` / ``version`` / ``status``) therefore saw "unknown
mode", "0 reqs", "no latency data", or "version not reported" purely from which
proxy runtime served the request. These tests pin both emitters to the same
contract via the shared ``_health_contract_core`` source of truth.
"""

from __future__ import annotations

from tokenpak.proxy.stats import _health_contract_core, build_health_response

# The canonical CLI-facing key set every /health emitter MUST expose.
CONTRACT_KEYS = {
    "status",
    "version",
    "uptime_seconds",
    "compilation_mode",
    "requests_total",
    "python_version",
    "stats",
    "latency",
}


def _build_sync_health(**overrides) -> dict:
    kwargs = dict(
        session={"requests": 7, "canon_hits": 1, "input_tokens": 500},
        compilation_mode="hybrid",
        vault_info={"available": True, "blocks": 3, "path": "/tmp/v"},
        router_info={},
        router_enabled=False,
        capsule_available=False,
        canon_available=False,
        skeleton_enabled=False,
        shadow_enabled=False,
        budget_total_tokens=0,
        tool_registry_stats={},
        tool_registry_available=False,
        term_resolver_enabled=False,
        term_resolver_available=False,
        term_resolver_top_k=0,
        term_resolver_max_bytes=0,
        query_expansion_enabled=False,
        upstream_timeout=30,
        provider_circuits={},
        request_latencies=[10, 20, 30],
        version="9.9.9",
        uptime_seconds=42.0,
    )
    kwargs.update(overrides)
    return build_health_response(**kwargs)


def test_contract_core_exposes_all_canonical_keys():
    core = _health_contract_core(
        status="ok",
        version="9.9.9",
        uptime_seconds=42.0,
        compilation_mode="hybrid",
        requests=7,
        request_latencies=[10, 20, 30],
    )
    assert CONTRACT_KEYS <= set(core)
    # requests surfaced both flat and nested so either reader form works.
    assert core["requests_total"] == 7
    assert core["stats"]["requests"] == 7
    # python_version is auto-filled (never a "?" / empty schema-drift fallback).
    assert core["python_version"]
    assert core["latency"]["samples"] == 3


def test_sync_emitter_exposes_full_contract():
    h = _build_sync_health()
    assert CONTRACT_KEYS <= set(h)
    # version / uptime threaded through (previously absent → "version not reported").
    assert h["version"] == "9.9.9"
    assert h["uptime_seconds"] == 42.0
    # mode / requests / latency stay real (no "unknown mode" / "0 reqs").
    assert h["compilation_mode"] == "hybrid"
    assert h["requests_total"] == 7
    assert h["stats"]["requests"] == 7
    assert h["latency"]["samples"] == 3


def test_sync_emitter_keeps_rich_stats_block():
    # The merge must not clobber the richer session stats the sync path builds.
    h = _build_sync_health()
    assert "input_tokens" in h["stats"]
    assert h["stats"]["input_tokens"] == 500


def test_sync_emitter_back_compat_defaults():
    # Legacy callers that omit version/uptime still produce a valid contract.
    h = _build_sync_health(version="", uptime_seconds=0.0)
    assert h["version"] == ""
    assert h["uptime_seconds"] == 0.0
    assert CONTRACT_KEYS <= set(h)


def test_async_emitter_exposes_full_contract():
    from tokenpak.proxy.server import ProxyServer

    srv = ProxyServer(host="127.0.0.1", port=18931, compilation_mode="hybrid")
    with srv._session_lock:
        srv.session["requests"] = 4
    h = srv.health()
    assert CONTRACT_KEYS <= set(h)
    # async path previously lacked these three → CLI saw drift artifacts.
    assert h["compilation_mode"] == "hybrid"
    assert h["stats"]["requests"] == 4
    assert "latency" in h
    # and it keeps its own operational fields.
    assert "circuit_breakers" in h
    assert "connection_pool" in h


def test_both_emitters_agree_on_contract_keys():
    from tokenpak.proxy.server import ProxyServer

    sync = _build_sync_health()
    srv = ProxyServer(host="127.0.0.1", port=18932, compilation_mode="hybrid")
    async_h = srv.health()
    # The CLI-facing contract subset is identical across emitters.
    assert CONTRACT_KEYS <= set(sync)
    assert CONTRACT_KEYS <= set(async_h)
    # requests is readable both flat and nested on each emitter.
    for h in (sync, async_h):
        assert h["requests_total"] == h["stats"]["requests"]
