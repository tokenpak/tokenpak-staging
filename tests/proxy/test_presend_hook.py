"""Pre-send extension call site (``tokenpak-presend/1``) in the real proxy.

A threaded ProxyServer forwards to a local stub upstream; no provider is
called. The extension is a test double unless tokenpak_paid is importable,
in which case the last tests drive the real Pro Dispatch Reroute module.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.needs_proxy

from tests.proxy._proxy_subprocess import free_port
from tokenpak.proxy import presend_hook
from tokenpak.proxy import server as proxy_server_module
from tokenpak.proxy.server import ProxyServer

_received: list = []
_order: list = []


class _Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _received.append((self.path, body))
        _order.append("upstream")
        model = json.loads(body).get("model")
        if self.path.endswith("/responses"):
            payload = {"id": "resp", "model": model, "usage": {
                "input_tokens": 900, "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 40, "output_tokens_details": {"reasoning_tokens": 12}}}
        else:
            payload = {"id": "msg", "type": "message", "model": model, "content": [],
                       "usage": {"input_tokens": 900, "output_tokens": 40,
                                 "cache_read_input_tokens": 0,
                                 "cache_creation_input_tokens": 0}}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture(scope="module")
def upstream_port():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture(scope="module")
def proxy():
    server = ProxyServer(host="127.0.0.1", port=free_port())
    server.start(blocking=False)
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", server.port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.05)
    yield server
    server.stop()


class _Extension:
    def __init__(self, decide=None):
        self.calls: list = []
        self.observed: list = []
        self.decide = decide or (lambda request: {"action": "forward", "decision": "stay"})

    def presend(self, request):
        self.calls.append(request)
        _order.append("presend")
        return self.decide(request)

    def observe(self, observation):
        self.observed.append(observation)


@pytest.fixture
def extension(monkeypatch):
    monkeypatch.setattr(
        proxy_server_module, "INTERCEPT_HOSTS",
        set(proxy_server_module.INTERCEPT_HOSTS) | {"127.0.0.1"},
    )
    # The stub host has no provider identity; parse its usage by path, as a
    # real provider host would be.
    real_usage = proxy_server_module._safe_provider_usage_observation

    def usage_by_path(provider, usage, request_body):
        if provider == "unknown" and isinstance(usage, dict):
            provider = "openai" if "input_tokens_details" in usage else "anthropic"
        return real_usage(provider, usage, request_body)

    monkeypatch.setattr(proxy_server_module, "_safe_provider_usage_observation", usage_by_path)
    _received.clear()
    _order.clear()
    installed = []

    def install(decide=None):
        ext = _Extension(decide)
        presend_hook._reset_for_tests(presend_hook._Hooks(ext.presend, ext.observe))
        installed.append(ext)
        return ext

    yield install
    presend_hook._reset_for_tests(None)


def _send(proxy, upstream_port, path, body, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=20)
    sent = {"Content-Type": "application/json", "x-api-key": "sk-tokenpak-presend-test"}
    if path.endswith("/responses"):
        sent = {"Content-Type": "application/json", "Authorization": "Bearer sk-presend-test"}
    sent.update(headers or {})
    conn.request("POST", f"http://127.0.0.1:{upstream_port}{path}", body=body, headers=sent)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def _messages(model="claude-sonnet-5", effort="max", text="hello", pad=0):
    return ('{"model": "%s", "max_tokens": 64, "output_config": {"effort": "%s"},'
            ' "messages": [{"role": "user", "content": "%s"}]}'
            % (model, effort, text + "z" * pad)).encode()


def _responses(model="gpt-5.6-sol", effort="xhigh"):
    return json.dumps({"model": model, "reasoning": {"effort": effort},
                       "input": [{"role": "user", "content": "hello"}]}).encode()


def _rewrite(model=None, effort=None):
    def decide(request):
        routing = {}
        if model:
            routing["model"] = model
        if effort:
            routing["effort"] = effort
        return {"action": "rewrite", "set": routing, "decision": "reroute"}

    return decide


CC = {"X-Claude-Code-Session-Id": "cc-session-1"}


def test_extension_runs_once_before_upstream_and_reroutes_the_same_request(
    proxy, upstream_port, extension,
):
    ext = extension(_rewrite(model="claude-haiku-4-5", effort="xhigh"))
    status, _ = _send(proxy, upstream_port, "/v1/messages", _messages(), CC)
    assert status == 200
    assert len(ext.calls) == 1 and _order == ["presend", "upstream"]
    sent = json.loads(_received[-1][1])
    assert (sent["model"], sent["output_config"]["effort"]) == ("claude-haiku-4-5", "xhigh")
    (observed,) = ext.observed
    assert observed["applied"] is True and observed["served_model"] == "claude-haiku-4-5"
    assert observed["usage"]["input_tokens"] == 900 and observed["usage"]["output_tokens"] == 40
    assert observed["usage"]["usage_source"] == "provider_usage_object"


def test_responses_protocol_reroutes_model_and_effort(proxy, upstream_port, extension):
    extension(_rewrite(model="gpt-5.4", effort="high"))
    status, _ = _send(proxy, upstream_port, "/v1/responses", _responses())
    assert status == 200
    sent = json.loads(_received[-1][1])
    assert (sent["model"], sent["reasoning"]["effort"]) == ("gpt-5.4", "high")


def test_stay_on_the_claude_code_route_forwards_the_client_bytes(
    proxy, upstream_port, extension,
):
    extension()
    body = _messages(text="exact  bytes \\u00e9")
    _send(proxy, upstream_port, "/v1/messages", body, CC)
    assert _received[-1][1] == body


def test_stay_matches_the_proxy_without_any_extension(proxy, upstream_port, extension):
    body = _responses()
    presend_hook._reset_for_tests(None)
    _send(proxy, upstream_port, "/v1/responses", body)
    baseline = _received[-1][1]
    extension()
    _send(proxy, upstream_port, "/v1/responses", body)
    assert _received[-1][1] == baseline


def test_extension_sees_no_credentials(proxy, upstream_port, extension):
    ext = extension()
    body = _messages()
    _send(proxy, upstream_port, "/v1/messages", body, {**CC, "X-Tokenpak-Agent": "agent-a"})
    assert ext.calls[0]["parsed"] == json.loads(body)  # the guard's parse, reused
    headers = ext.calls[0]["headers"]
    assert "x-api-key" not in headers and "authorization" not in headers
    assert headers["x-claude-code-session-id"] == "cc-session-1"
    assert ext.calls[0]["session_id"] == "cc-session-1"
    assert ext.calls[0]["agent_id"] == "agent-a" and ext.calls[0]["guard"]["state"] == "allow"


@pytest.mark.parametrize("routing,reason", [
    ({"messages": []}, "malformed"),
    ({"model": "claude-haiku-4-5", "max_tokens": 1}, "malformed"),
    ({"effort": "ultra-plus"}, "rewrite_refused"),
])
def test_only_routing_values_are_accepted(proxy, upstream_port, extension, routing, reason):
    ext = extension(lambda request: {"action": "rewrite", "set": routing})
    body = _messages()
    _send(proxy, upstream_port, "/v1/messages", body, CC)
    assert _received[-1][1] == body
    assert ext.observed[0]["applied"] is False and ext.observed[0]["revert_reason"] == reason


def test_missing_effort_field_is_not_added(proxy, upstream_port, extension):
    ext = extension(_rewrite(effort="high"))
    body = b'{"model": "claude-sonnet-5", "max_tokens": 64, "messages": []}'
    _send(proxy, upstream_port, "/v1/messages", body, CC)
    assert _received[-1][1] == body
    assert ext.observed[0]["revert_reason"] == "rewrite_refused"


def test_extension_error_forwards_unchanged(proxy, upstream_port, extension):
    def broken(request):
        raise RuntimeError("boom")

    extension(broken)
    body = _messages()
    status, _ = _send(proxy, upstream_port, "/v1/messages", body, CC)
    assert status == 200 and _received[-1][1] == body


def test_extension_timeout_forwards_unchanged(proxy, upstream_port, extension, monkeypatch):
    monkeypatch.setenv("TOKENPAK_PRESEND_TIMEOUT_MS", "20")

    def slow(request):
        time.sleep(0.3)
        return _rewrite(model="claude-haiku-4-5")(request)

    ext = extension(slow)
    body = _messages()
    _send(proxy, upstream_port, "/v1/messages", body, CC)
    assert _received[-1][1] == body
    assert ext.observed[0]["applied"] is False and ext.observed[0]["revert_reason"] == "timeout"


def test_disabled_or_absent_extension_changes_nothing(proxy, upstream_port, extension, monkeypatch):
    ext = extension(_rewrite(model="claude-haiku-4-5"))
    monkeypatch.setenv("TOKENPAK_PRESEND_HOOK", "0")
    body = _messages()
    _send(proxy, upstream_port, "/v1/messages", body, CC)
    assert not ext.calls and _received[-1][1] == body


def test_hard_stop_blocks_before_the_extension(proxy, upstream_port, extension):
    ext = extension(_rewrite(model="claude-sonnet-5"))
    body = _messages(model="claude-haiku-4-5", pad=1_200_000)  # over a 200k window
    status, data = _send(proxy, upstream_port, "/v1/messages", body, CC)
    assert status == 402 and b"spend_guard" in data
    assert not ext.calls and not _received


def test_rewrite_that_the_guard_would_block_is_refused(proxy, upstream_port, extension):
    ext = extension(_rewrite(model="claude-haiku-4-5"))
    body = _messages(model="claude-sonnet-5", pad=740_000)  # fits 1M, not 90% of 200k
    status, _ = _send(proxy, upstream_port, "/v1/messages", body, CC)
    assert status == 200 and _received[-1][1] == body
    assert ext.observed[0]["revert_reason"] == "guard_recheck"


def test_apply_routing_fields_changes_only_routing_values():
    before = b'{ "model" :"a",\n "output_config": {"x": 1, "effort":"max"}, "t": "\\u00e9 model"}'
    after = presend_hook.apply_routing_fields(before, model="b", effort="xhigh")
    assert after == before.replace(b'"a"', b'"b"').replace(b'"max"', b'"xhigh"')
    responses = b'{"reasoning": {"effort": "high", "summary": "auto"}, "model": "m"}'
    assert presend_hook.apply_routing_fields(responses, model=None, effort="low") == (
        responses.replace(b'"high"', b'"low"'))
    for bad in (b'{"model": "a", "model": "b"}', b'{"model": "a"} x', b"\xff", b"[1]"):
        with pytest.raises(presend_hook.RoutingRewriteError):
            presend_hook.apply_routing_fields(bad, model="c", effort=None)


# -- with the real Pro extension, when installed ---------------------------------


@pytest.fixture
def pro(tmp_path, monkeypatch, extension):
    presend = pytest.importorskip("tokenpak_paid.reroute.presend")
    from tokenpak_paid.reroute.presend_receipts import ReceiptJournal

    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "reroute:\n"
        "  mode: auto\n"
        "  theta_usd: 0.0\n"
        "  prices:\n"
        "    claude-opus-5: {provider: anthropic, input: 5.0, output: 25.0,"
        " cache_read: 0.5, cache_write: 6.25, max_context: 1000000}\n"
        "    claude-sonnet-5: {provider: anthropic, input: 2.0, output: 10.0,"
        " cache_read: 0.2, cache_write: 2.5, max_context: 1000000}\n"
        "  targets:\n"
        "    - {id: t, provider: anthropic, from_models: [claude-opus-5],"
        " model: claude-sonnet-5, when: always}\n"
    )
    monkeypatch.setenv("TOKENPAK_REROUTE_POLICY", str(policy))
    journal = ReceiptJournal(directory=lambda: tmp_path / "receipts")
    runtime = presend.PresendRuntime(journal=journal, entitlement_check=lambda: True)
    monkeypatch.setattr(presend, "_runtime", runtime)
    presend_hook._reset_for_tests(
        presend_hook._Hooks(presend.presend_hook, presend.observe_response, presend.warm_up)
    )
    runtime.warm_up()
    return runtime, journal, tmp_path / "receipts"


def test_real_pro_reroutes_and_writes_one_receipt(proxy, upstream_port, pro):
    runtime, journal, receipts = pro
    body = _messages(model="claude-opus-5", pad=20_000)
    _send(proxy, upstream_port, "/v1/messages", body, CC)
    assert json.loads(_received[-1][1])["model"] == "claude-sonnet-5"
    journal.flush()
    (line,) = next(receipts.glob("*.jsonl")).read_text().splitlines()
    receipt = json.loads(line)
    assert receipt["action"] == "rewritten" and receipt["realized"]["status"] == "observed"
    assert "zzzz" not in line


def test_real_pro_stay_is_byte_identical(proxy, upstream_port, pro):
    body = _messages(model="claude-sonnet-5", text="keep  me")
    _send(proxy, upstream_port, "/v1/messages", body, CC)
    assert _received[-1][1] == body
