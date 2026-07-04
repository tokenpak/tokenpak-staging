# SPDX-License-Identifier: Apache-2.0
"""Deterministic, offline regression tests for the MCP proxy transport layer.

Reproduces the transient MCP-to-local-proxy timeout reported in
``p2-companion-mcp-proxy-timeout-under-load``: under a busy local proxy a
lightweight ``/tpk/v1/*`` call could exceed the fixed 5s deadline and be
mislabeled ``proxy_unreachable`` even though the proxy was alive (a direct
``curl`` moments later succeeded).

Unlike ``test_mcp_tools.py`` these tests do NOT require a running proxy — they
stub ``urlopen`` to model a busy/slow proxy deterministically, per the packet's
"synthetic/local fixture" requirement. The behaviours pinned here:

* a transient GET timeout is smoothed by one bounded retry;
* an exhausted timeout fails closed but is classified ``proxy_timeout`` (alive
  but slow), distinct from ``proxy_unreachable`` (down);
* POSTs (non-idempotent writes) are never retried;
* a definitive HTTP status (4xx/5xx) is never retried.
"""

from __future__ import annotations

import io
import json
import socket
import urllib.error

import pytest

from tokenpak.companion.config import CompanionConfig
from tokenpak.companion.mcp import tools
from tokenpak.companion.mcp.tools import (
    CompanionState,
    _classify_transport_error,
    _handle_check_budget,
    _handle_journal_write,
    _proxy_get,
    _proxy_post,
)


@pytest.fixture(autouse=True)
def _fast_bounded_retries(monkeypatch):
    """Enable the bounded GET retry but zero the backoff so tests never sleep."""
    monkeypatch.setenv("TOKENPAK_PROXY_RETRIES", "1")
    monkeypatch.setenv("TOKENPAK_PROXY_RETRY_BACKOFF", "0")
    monkeypatch.setenv("TOKENPAK_PROXY_TIMEOUT", "5")


class _FakeResp:
    """Minimal context-manager stand-in for an ``http.client.HTTPResponse``."""

    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


def _install_urlopen(monkeypatch, side_effect):
    """Replace ``tools._url_req.urlopen`` with a call-counting fake.

    ``side_effect`` is a list; each entry is either an exception (raised) or a
    ``(status, payload)`` tuple (returned as a ``_FakeResp``). The final entry
    repeats for any further calls.
    """
    calls = {"n": 0}
    seq = list(side_effect)

    def fake_urlopen(req, timeout=None):
        idx = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        item = seq[idx]
        if isinstance(item, BaseException):
            raise item
        status, payload = item
        return _FakeResp(status, payload)

    monkeypatch.setattr(tools._url_req, "urlopen", fake_urlopen)
    return calls


def _state(tmp_path, session_id: str = "sess-xyz") -> CompanionState:
    cfg = CompanionConfig(journal_dir=tmp_path, budget_daily_usd=10.0)
    return CompanionState(config=cfg, session_id=session_id)


# ---------------------------------------------------------------------------
# Error classification: timeout (alive but slow) vs unreachable (down)
# ---------------------------------------------------------------------------


def test_classify_socket_timeout_is_proxy_timeout():
    assert _classify_transport_error(socket.timeout("timed out"))["error"] == "proxy_timeout"


def test_classify_timeouterror_is_proxy_timeout():
    assert _classify_transport_error(TimeoutError())["error"] == "proxy_timeout"


def test_classify_urlerror_wrapping_timeout_is_proxy_timeout():
    exc = urllib.error.URLError(socket.timeout("timed out"))
    assert _classify_transport_error(exc)["error"] == "proxy_timeout"


def test_classify_connection_refused_is_unreachable():
    exc = ConnectionRefusedError("[Errno 111] Connection refused")
    assert _classify_transport_error(exc)["error"] == "proxy_unreachable"


def test_classify_urlerror_wrapping_refused_is_unreachable():
    exc = urllib.error.URLError(ConnectionRefusedError("refused"))
    assert _classify_transport_error(exc)["error"] == "proxy_unreachable"


# ---------------------------------------------------------------------------
# GET retry smooths a transient stall (the reported incident)
# ---------------------------------------------------------------------------


def test_get_retries_and_recovers_after_transient_timeout(monkeypatch):
    calls = _install_urlopen(monkeypatch, [socket.timeout("timed out"), (200, {"ok": True})])
    status, body = _proxy_get("/tpk/v1/budget")
    assert status == 200
    assert body == {"ok": True}
    assert calls["n"] == 2  # first attempt timed out, retry succeeded


def test_check_budget_recovers_under_busy_proxy(monkeypatch, tmp_path):
    _install_urlopen(
        monkeypatch,
        [socket.timeout("timed out"), (200, {"session_cost_usd": 0.0, "daily": {}})],
    )
    out = json.loads(_handle_check_budget(_state(tmp_path), {}))
    assert "error" not in out
    # honest-scope note is only attached on a successful proxy response
    assert out.get("_tokenpak_scope")


# ---------------------------------------------------------------------------
# Exhausted retries fail closed, correctly classified
# ---------------------------------------------------------------------------


def test_get_timeout_exhausted_is_proxy_timeout(monkeypatch):
    calls = _install_urlopen(monkeypatch, [socket.timeout("timed out")])
    status, body = _proxy_get("/tpk/v1/budget")
    assert status == 0
    assert body["error"] == "proxy_timeout"
    assert calls["n"] == 2  # initial + one retry, both timed out


def test_check_budget_timeout_surfaces_proxy_timeout(monkeypatch, tmp_path):
    _install_urlopen(monkeypatch, [socket.timeout("timed out")])
    out = json.loads(_handle_check_budget(_state(tmp_path), {}))
    assert out["error"] == "proxy_timeout"


def test_get_unreachable_stays_unreachable(monkeypatch):
    _install_urlopen(monkeypatch, [ConnectionRefusedError("refused")])
    status, body = _proxy_get("/tpk/v1/budget")
    assert status == 0
    assert body["error"] == "proxy_unreachable"


# ---------------------------------------------------------------------------
# POST is never retried — a write may have landed before a read timeout
# ---------------------------------------------------------------------------


def test_post_not_retried_on_timeout(monkeypatch):
    calls = _install_urlopen(monkeypatch, [socket.timeout("timed out")])
    status, body = _proxy_post("/tpk/v1/journal/x/entry", {"content": "hi"})
    assert status == 0
    assert body["error"] == "proxy_timeout"
    assert calls["n"] == 1  # exactly one attempt


def test_journal_write_timeout_not_retried(monkeypatch, tmp_path):
    calls = _install_urlopen(monkeypatch, [socket.timeout("timed out")])
    out = json.loads(_handle_journal_write(_state(tmp_path), {"content": "note"}))
    assert out["error"] == "proxy_timeout"
    assert calls["n"] == 1


def test_journal_write_unreachable_not_retried(monkeypatch, tmp_path):
    calls = _install_urlopen(monkeypatch, [ConnectionRefusedError("refused")])
    out = json.loads(_handle_journal_write(_state(tmp_path), {"content": "note"}))
    assert out["error"] == "proxy_unreachable"
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# A definitive HTTP status is an answer, not a transient failure
# ---------------------------------------------------------------------------


def test_http_error_status_not_retried(monkeypatch):
    err = urllib.error.HTTPError(
        "http://127.0.0.1:8766/tpk/v1/budget", 503, "busy", {}, io.BytesIO(b'{"error":"busy"}')
    )
    calls = _install_urlopen(monkeypatch, [err])
    status, body = _proxy_get("/tpk/v1/budget")
    assert status == 503
    assert body == {"error": "busy"}
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Retry is tunable — 0 restores the historical single-shot behaviour
# ---------------------------------------------------------------------------


def test_retries_env_zero_disables_get_retry(monkeypatch):
    monkeypatch.setenv("TOKENPAK_PROXY_RETRIES", "0")
    calls = _install_urlopen(monkeypatch, [socket.timeout("timed out")])
    status, _body = _proxy_get("/tpk/v1/budget")
    assert status == 0
    assert calls["n"] == 1


def test_retries_env_is_clamped(monkeypatch):
    monkeypatch.setenv("TOKENPAK_PROXY_RETRIES", "999")
    calls = _install_urlopen(monkeypatch, [socket.timeout("timed out")])
    _proxy_get("/tpk/v1/budget")
    assert calls["n"] == 6  # initial + clamp(5) retries
