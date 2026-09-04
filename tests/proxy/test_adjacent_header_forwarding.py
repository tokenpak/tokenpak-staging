"""Internal request markers stay inside every proxy forwarding implementation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tokenpak.proxy.circuit_breaker import _sanitize_headers
from tokenpak.proxy.request import HTTPProxy, ProxyRequest
from tokenpak.proxy.server import _filter_allowlisted_forward_headers
from tokenpak.proxy.server_async import _build_forward_headers
from tokenpak.proxy.spend_guard.classifier import is_internal_header

INTERNAL_HEADERS = {
    "X-Tokenpak-Managed": "1",
    "X-Tokenpak-Custom-Marker": "internal",
    "X-Tpk-Trace-Id": "trace",
}
SAFE_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer test-token",
    "X-Client-Marker": "client",
}


def _headers_with_internal_markers() -> dict[str, str]:
    return {**SAFE_HEADERS, **INTERNAL_HEADERS}


def _assert_only_safe_headers(headers: dict[str, str]) -> None:
    assert not any(is_internal_header(name) for name in headers)
    for name, value in SAFE_HEADERS.items():
        assert headers[name] == value


def test_async_forward_headers_strip_internal_markers() -> None:
    request = MagicMock()
    request.headers = _headers_with_internal_markers()

    forwarded = _build_forward_headers(request, "https://example.test/v1/messages")

    _assert_only_safe_headers(forwarded)
    assert forwarded["host"] == "example.test"


def test_circuit_breaker_sanitizer_strips_internal_markers() -> None:
    forwarded = _sanitize_headers(_headers_with_internal_markers())

    _assert_only_safe_headers(forwarded)


def test_request_forwarder_strips_internal_markers_and_preserves_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"response"

        def getheaders(self) -> list[tuple[str, str]]:
            return [("Content-Type", "application/json")]

    def _urlopen(request, timeout: int):
        captured["headers"] = dict(request.header_items())
        captured["body"] = request.data
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    body = b'{"messages":[]}'
    response = HTTPProxy().handle_request(
        ProxyRequest(
            method="POST",
            url="https://example.test/v1/messages",
            headers=_headers_with_internal_markers(),
            body=body,
        )
    )

    forwarded = captured["headers"]
    assert isinstance(forwarded, dict)
    assert not any(is_internal_header(name) for name in forwarded)
    assert forwarded["Content-type"] == "application/json"
    assert forwarded["Authorization"] == "Bearer test-token"
    assert forwarded["X-client-marker"] == "client"
    assert captured["body"] == body
    assert response.body == b"response"


def test_sync_allowlist_filter_strips_internal_markers() -> None:
    raw = _headers_with_internal_markers()
    allowlist = frozenset(name.lower() for name in raw)

    forwarded = _filter_allowlisted_forward_headers(raw, allowlist)

    assert not any(is_internal_header(name) for name in forwarded)
    assert forwarded == {name.lower(): value for name, value in SAFE_HEADERS.items()}
