# SPDX-License-Identifier: Apache-2.0
"""Authoritative custom-provider registration and routing regressions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from tokenpak.proxy.adapters import build_default_registry
from tokenpak.proxy.custom_providers import (
    CustomProvider,
    build_custom_adapters,
    get_provider_display_list,
    load_custom_providers,
)
from tokenpak.proxy.router import INTERCEPT_HOSTS, ProviderRouter, should_intercept
from tokenpak.proxy.server import _format_proxy_startup_banner


def _deepseek() -> CustomProvider:
    return CustomProvider(
        name="deepseek",
        endpoint="https://api.deepseek.com/v1",
        format="openai-chat",
        api_key_env="DEEPSEEK_API_KEY",
        hostname="api.deepseek.com",
    )


def test_loader_calls_canonical_config_module() -> None:
    config = {
        "providers": {
            "deepseek": {
                "endpoint": "https://api.deepseek.com/v1",
                "format": "openai",
                "api_key_env": "DEEPSEEK_API_KEY",
            }
        }
    }
    with patch("tokenpak.core.config_loader.load_config", return_value=config) as load_config:
        providers = load_custom_providers()

    load_config.assert_called_once_with()
    assert [provider.name for provider in providers] == ["deepseek"]


def test_loader_skips_unsupported_urls_and_duplicate_hostnames() -> None:
    config = {
        "providers": {
            "primary": {
                "endpoint": "https://api.example.test/v1",
                "format": "openai",
            },
            "duplicate": {
                "endpoint": "https://API.EXAMPLE.TEST/alternate",
                "format": "anthropic",
            },
            "wrong-scheme": {
                "endpoint": "ftp://files.example.test/v1",
                "format": "openai",
            },
            "bad-port": {
                "endpoint": "https://port.example.test:not-a-port/v1",
                "format": "openai",
            },
        }
    }

    with patch("tokenpak.core.config_loader.load_config", return_value=config):
        providers = load_custom_providers()

    assert [provider.name for provider in providers] == ["primary"]


def test_custom_hostname_wins_before_generic_wire_format() -> None:
    registry = build_default_registry()
    adapters = build_custom_adapters([_deepseek()], registry)

    detected = registry.detect(
        "https://api.deepseek.com/v1/chat/completions",
        {"Host": "api.deepseek.com:443"},
        b'{"model":"deepseek-chat","messages":[]}',
    )

    assert detected is adapters[0]
    assert detected.source_format == "custom-deepseek"


def test_custom_hostname_matching_is_exact_and_port_safe() -> None:
    registry = build_default_registry()
    adapter = build_custom_adapters([_deepseek()], registry)[0]

    assert adapter.detect("/v1/chat/completions", {"Host": "API.DEEPSEEK.COM:443"}, None)
    assert not adapter.detect(
        "https://notapi.deepseek.com/v1/chat/completions", {"Host": "notapi.deepseek.com"}, None
    )
    assert not adapter.detect(
        "https://api.deepseek.com.evil.test/v1/chat/completions",
        {"Host": "api.deepseek.com.evil.test"},
        None,
    )


def test_registered_adapter_upstream_is_the_router_selection() -> None:
    registry = build_default_registry()
    adapter = build_custom_adapters([_deepseek()], registry)[0]
    router = ProviderRouter(
        custom_urls={adapter.source_format: adapter.get_default_upstream()},
        custom_hosts={"api.deepseek.com": adapter.source_format},
    )

    route = router.route(
        "/v1/chat/completions",
        {"Host": "api.deepseek.com:443"},
        b'{"model":"deepseek-chat","messages":[]}',
    )

    assert route.provider == "custom-deepseek"
    assert route.base_url == adapter.get_default_upstream()
    assert route.full_url == "https://api.deepseek.com/v1/chat/completions"


def test_intercept_matching_is_exact() -> None:
    INTERCEPT_HOSTS.add("evil.com")
    try:
        assert should_intercept("https://evil.com/v1/chat/completions")
        assert not should_intercept("https://notevil.com/v1/chat/completions")
        assert not should_intercept("https://evil.com.attacker.test/v1/chat/completions")
    finally:
        INTERCEPT_HOSTS.discard("evil.com")


def test_intercept_matching_accepts_an_explicit_host_collection() -> None:
    hosts = {"127.0.0.1"}

    assert should_intercept("http://127.0.0.1:18971/v1/messages", hosts)
    assert not should_intercept("http://127.0.0.2:18971/v1/messages", hosts)


def test_display_lists_only_successfully_registered_custom_providers() -> None:
    configured = [
        _deepseek(),
        CustomProvider(
            name="unsupported",
            endpoint="https://unsupported.example/v1",
            format="missing-format",
            api_key_env="",
            hostname="unsupported.example",
        ),
    ]
    registry = build_default_registry()
    adapters = build_custom_adapters(configured, registry)
    registered_formats = {adapter.source_format for adapter in adapters}
    registered = [
        provider for provider in configured if f"custom-{provider.name}" in registered_formats
    ]

    display = get_provider_display_list(registry, registered)

    assert "deepseek (custom)" in display
    assert "unsupported (custom)" not in display


def test_proxy_config_exposes_configured_and_registered_counts(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
providers:
  deepseek:
    endpoint: https://api.deepseek.com/v1
    format: openai
    api_key_env: DEEPSEEK_API_KEY
  unsupported:
    endpoint: https://unsupported.example/v1
    format: missing-format
""".lstrip(),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "TOKENPAK_CONFIG": str(config_path),
            "TOKENPAK_HOME": str(tmp_path / "home"),
            "HOME": str(tmp_path),
        }
    )
    code = """
import json
from tokenpak.proxy import config
print(json.dumps({
    "configured": config.CUSTOM_PROVIDER_CONFIGURED_COUNT,
    "registered": config.CUSTOM_PROVIDER_REGISTERED_COUNT,
    "route": config.CUSTOM_PROVIDER_ROUTES.get("custom-deepseek"),
    "host": config.CUSTOM_PROVIDER_HOSTS.get("api.deepseek.com"),
    "detected": config.ADAPTER_REGISTRY.detect(
        "https://api.deepseek.com/v1/chat/completions", {}, None
    ).source_format,
    "display": config.PROVIDER_DISPLAY,
}))
"""

    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    payload = json.loads(completed.stdout)

    assert payload == {
        "configured": 2,
        "registered": 1,
        "route": "https://api.deepseek.com/v1",
        "host": "custom-deepseek",
        "detected": "custom-deepseek",
        "display": payload["display"],
    }
    assert "deepseek (custom)" in payload["display"]


def test_startup_banner_reports_registered_over_configured(tmp_path: Path) -> None:
    banner = _format_proxy_startup_banner(
        host="127.0.0.1",
        port=8766,
        profile="balanced",
        mode="hybrid",
        mode_description="test mode",
        provider_display="openai-chat, deepseek (custom)",
        custom_configured=2,
        custom_registered=1,
        pid=1234,
        pid_path=tmp_path / "proxy.pid",
    )

    assert "Providers:  openai-chat, deepseek (custom)" in banner
    assert "Custom:     1/2 registered" in banner


def test_proxy_server_wires_registered_custom_routes(monkeypatch, tmp_path: Path) -> None:
    from tokenpak.proxy import config
    from tokenpak.proxy import server as server_module

    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path))
    monkeypatch.setattr(
        config,
        "CUSTOM_PROVIDER_ROUTES",
        {"custom-deepseek": "https://api.deepseek.com/v1"},
    )
    monkeypatch.setattr(
        config,
        "CUSTOM_PROVIDER_HOSTS",
        {"api.deepseek.com": "custom-deepseek"},
    )
    monkeypatch.setattr(config, "CUSTOM_PROVIDER_CONFIGURED_COUNT", 2)
    monkeypatch.setattr(config, "CUSTOM_PROVIDER_REGISTERED_COUNT", 1)
    monkeypatch.setattr(server_module, "_create_memory_guard", lambda: None)
    monkeypatch.setattr(server_module, "_memory_guard_configuration_status", lambda: {})
    monkeypatch.setattr(server_module, "_DbMonitor", lambda _path: None)

    proxy = server_module.ProxyServer()
    try:
        route = proxy.router.route(
            "/v1/chat/completions",
            {"Host": "api.deepseek.com:443"},
            b'{"model":"deepseek-chat","messages":[]}',
        )
        assert route.provider == "custom-deepseek"
        assert route.full_url == "https://api.deepseek.com/v1/chat/completions"
        assert proxy.custom_provider_configured_count == 2
        assert proxy.custom_provider_registered_count == 1
    finally:
        proxy._connection_pool.close()
