# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from urllib.error import HTTPError

from tokenpak.companion.mcp import tools as mcp_tools


class _Response:
    status = 200

    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._body


def test_budget_endpoint_gets_bounded_retry(monkeypatch):
    calls: list[float] = []

    def fake_urlopen(req, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise TimeoutError("timed out")
        return _Response({"session_cost_usd": 0.0})

    monkeypatch.setattr(mcp_tools._url_req, "urlopen", fake_urlopen)
    monkeypatch.setattr(mcp_tools._time, "sleep", lambda _seconds: None)

    status, body = mcp_tools._proxy_get("/tpk/v1/budget")

    assert status == 200
    assert body["session_cost_usd"] == 0.0
    assert calls == [6.0, 12.0]


def test_session_info_timeout_reports_terminal_attempts(monkeypatch):
    calls: list[float] = []

    def fake_urlopen(req, timeout):
        calls.append(timeout)
        raise TimeoutError("timed out")

    monkeypatch.setattr(mcp_tools._url_req, "urlopen", fake_urlopen)
    monkeypatch.setattr(mcp_tools._time, "sleep", lambda _seconds: None)

    status, body = mcp_tools._proxy_get("/tpk/v1/session/info")

    assert status == 0
    assert body["error"] == "proxy_unreachable"
    assert body["endpoint"] == "/tpk/v1/session/info"
    assert body["attempts"] == 2
    assert "timeouts_seconds=[6.0, 12.0]" in body["detail"]
    assert calls == [6.0, 12.0]


def test_non_status_endpoints_keep_single_default_timeout(monkeypatch):
    calls: list[float] = []

    def fake_urlopen(req, timeout):
        calls.append(timeout)
        raise TimeoutError("timed out")

    monkeypatch.setattr(mcp_tools._url_req, "urlopen", fake_urlopen)

    status, body = mcp_tools._proxy_get("/tpk/v1/capsules")

    assert status == 0
    assert body["error"] == "proxy_unreachable"
    assert body["attempts"] == 1
    assert calls == [5.0]


def test_http_error_is_not_retried(monkeypatch):
    calls: list[float] = []

    def fake_urlopen(req, timeout):
        calls.append(timeout)
        raise HTTPError(
            url=req.full_url,
            code=503,
            msg="service unavailable",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr(mcp_tools._url_req, "urlopen", fake_urlopen)

    status, body = mcp_tools._proxy_get("/tpk/v1/budget")

    assert status == 503
    assert body["error"] == "http_503"
    assert calls == [6.0]
