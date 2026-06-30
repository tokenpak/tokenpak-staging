# SPDX-License-Identifier: Apache-2.0
"""First-run version command truth checks."""

from __future__ import annotations

import json

from tokenpak import _cli_core


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


def test_version_probe_uses_health_endpoint(monkeypatch):
    calls: list[tuple[str, float | None]] = []

    def _urlopen(url: str, timeout: float | None = None) -> _Response:
        calls.append((url, timeout))
        return _Response(
            {
                "version": "1.10.0",
                "uptime_s": 125,
                "runtime": {"python_version": "3.12.3"},
                "config_hash": "sha256:test",
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)

    payload = _cli_core._get_proxy_version()

    assert calls == [(f"{_cli_core._PROXY_URL}/health", 3)]
    assert payload["version"] == "1.10.0"
    assert payload["uptime"] == 125
    assert payload["pythonVersion"] == "3.12.3"
    assert payload["configHash"] == "sha256:test"
